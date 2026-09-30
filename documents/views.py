import difflib
import re
from functools import wraps
from itertools import groupby

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.text import slugify
from django.views.decorators.http import require_POST

import calendar

from accounts.permissions import has_perm, require_perm, users_with_perm
from core.models import log_activity, notify
from projects.access import visible_projects
from projects.models import Project
from ai_engine.services import extract_priced_document, generate_content
from .access import (
    can_view_doc_slug, can_view_doc_type, is_financial_type,
    visible_doc_types, visible_documents,
)
from .models import DocumentType, Document, DocumentVersion, GeneratedFile, LineItem
from .pdf import render_document_pdf
from .pricing import (
    GST_MODES, build_context, render_body, replace_line_items, rows_from_ai,
    rows_from_post,
)
from .services import assign_number, default_template_for, generate_monthly_report


def block_financial_doc(view):
    """Guard single-document views, on two independent axes.

    **Type.** Financial documents (invoice / quotation / proposal) are off-limits
    to users with no route to that type — either blanket finance visibility or
    `view` on the type's own module.

    **Project.** A document is only as visible as the project it belongs to, so
    someone who is not on that project cannot read its contract by walking URLs.

    Both run before the wrapped view on one light query, and both matter even
    though the buttons are already hidden: a hand-crafted request bypasses a
    hidden button, not a decorator. Every view in this module that takes a
    document pk carries it.
    """
    @wraps(view)
    def wrapper(request, pk, *args, **kwargs):
        row = (Document.objects.filter(pk=pk)
               .values_list("document_type__slug", "project_id").first())
        if row is not None:
            slug, project_id = row
            if not can_view_doc_slug(request.user, slug):
                messages.error(request, "You don't have access to finance documents.")
                return redirect("core:dashboard")
            if not visible_projects(
                    Project.objects.filter(pk=project_id), request.user).exists():
                messages.error(
                    request, "That document belongs to a project you're not on.")
                return redirect("core:dashboard")
        return view(request, pk, *args, **kwargs)
    return wrapper

# Allowed status moves and the button labels shown for each move.
STATUS_FLOW = {
    Document.Status.DRAFT: [Document.Status.REVIEW],
    Document.Status.REVIEW: [Document.Status.APPROVED, Document.Status.DRAFT],
    Document.Status.APPROVED: [Document.Status.SENT, Document.Status.DRAFT],
    Document.Status.SENT: [Document.Status.DRAFT],
}
STATUS_ACTION_LABELS = {
    Document.Status.REVIEW: "Submit for review",
    Document.Status.APPROVED: "Approve",
    Document.Status.SENT: "Mark as sent",
    Document.Status.DRAFT: "Back to draft",
}
# Statuses that represent a sign-off, and so can't survive a content change.
SIGNED_OFF = (Document.Status.APPROVED, Document.Status.SENT)


def _parse_date(raw):
    """A date from a form field, or None. A blank or malformed value clears the
    field rather than raising — the review deadline is optional."""
    from datetime import date

    try:
        return date.fromisoformat((raw or "").strip())
    except (ValueError, AttributeError):
        return None


def _next_version_number(document):
    last = document.versions.first()
    return (last.version_number + 1) if last else 1


def _save_version(document, content_html, user, note):
    """Write a new version and make it current.

    Content that changes after sign-off invalidates the sign-off: an APPROVED
    or SENT document drops back to DRAFT, mirroring the rule work logs already
    follow (projects.views.worklog_edit). Returns (version, previous_status),
    where previous_status is the label it was reset FROM, or None if it wasn't.
    """
    version = DocumentVersion.objects.create(
        document=document,
        version_number=_next_version_number(document),
        content_html=content_html,
        note=note,
        created_by=user,
    )
    document.current_version = version
    fields = ["current_version", "updated_at"]
    was = None
    if document.status in SIGNED_OFF:
        was = document.get_status_display()
        document.status = Document.Status.DRAFT
        fields.append("status")
    document.save(update_fields=fields)
    return version, was


def _warn_if_reopened(request, document, previous_status):
    if previous_status:
        messages.info(
            request,
            f"“{document.title}” was {previous_status.lower()} — editing it sent "
            "it back to draft for re-approval.")


def _generate(document):
    """Run generation from the single brief plus server context (doc number)."""
    return generate_content(
        document.document_type, document.form_data,
        document.project.client, document.project,
        extra_context={"number": document.number or ""},
    )


def _generate_priced(document):
    """Draft a priced document: AI proposes rows, we own every number.

    The model's reply is turned into LineItems and the body is then *rendered*
    from those rows, so the table a client sees is the same data the editor
    edits. When the AI is unreachable the document is still created with its
    number, dates and an empty table — a draft to type into, not a failure.
    """
    drafted = extract_priced_document(
        document.document_type, document.form_data,
        document.project.client, document.project,
        extra_context={"number": document.number or ""},
    )
    replace_line_items(document, rows_from_ai(drafted["items"]))
    form_data = dict(document.form_data or {})
    # The create form collects issue_date, so an untouched date field arrives as
    # "" — present but empty. setdefault would happily keep that and ship a
    # dateless invoice, so test the value, not the key.
    if not form_data.get("issue_date"):
        form_data["issue_date"] = timezone.localdate().isoformat()
    if drafted["notes"] and not form_data.get("notes"):
        form_data["notes"] = drafted["notes"]
    document.form_data = form_data
    document.save(update_fields=["form_data", "updated_at"])
    return render_body(document)


@login_required
@require_perm("documents", "view")
def document_list(request):
    """Global document library: search + filter across every client/project."""
    show_archived = request.GET.get("archived") == "1"
    documents = (
        Document.objects.filter(is_archived=show_archived)
        .select_related("project", "project__client", "document_type", "current_version")
    )
    # Financial documents (invoices/quotations/proposals) are hidden from users
    # without finance visibility.
    documents = visible_documents(documents, request.user)
    # …and only on projects the viewer is on. Documents inherit their project's
    # visibility: a contract for a project you cannot open is not yours to read.
    documents = documents.filter(
        project__in=visible_projects(Project.objects.all(), request.user))
    q = request.GET.get("q", "").strip()
    if q:
        documents = documents.filter(
            Q(title__icontains=q) | Q(number__icontains=q)
            | Q(project__name__icontains=q) | Q(project__client__name__icontains=q)
        )
    status = request.GET.get("status", "")
    if status in Document.Status.values:
        documents = documents.filter(status=status)
    type_slug = request.GET.get("type", "")
    if type_slug:
        documents = documents.filter(document_type__slug=type_slug)

    # Group by project so the library reads like the project-first workspace:
    # ordering makes each project's documents consecutive for groupby().
    documents = documents.order_by(
        "project__client__name", "project__name", "-updated_at")
    doc_list = list(documents[:300])
    groups = [
        {"project": project, "documents": list(docs)}
        for project, docs in groupby(doc_list, key=lambda d: d.project)
    ]
    return render(request, "documents/list.html", {
        "groups": groups,
        "doc_total": len(doc_list),
        "q": q,
        "status": status,
        "type_slug": type_slug,
        "show_archived": show_archived,
        "statuses": Document.Status.choices,
        "doc_types": visible_doc_types(
            DocumentType.objects.filter(is_active=True), request.user),
        # For the "New document" picker — every project you could file under.
        "projects": visible_projects(
            Project.objects.filter(is_archived=False), request.user)
            .select_related("client").order_by("client__name", "name"),
    })


@login_required
@require_perm("documents", "create")
def document_create(request, project_pk, type_slug):
    project = get_object_or_404(
        visible_projects(Project.objects.select_related("client"), request.user),
        pk=project_pk)
    doc_type = get_object_or_404(DocumentType, slug=type_slug, is_active=True)
    if is_financial_type(doc_type) and not can_view_doc_type(request.user, doc_type):
        messages.error(request, "You don't have access to finance documents.")
        return redirect(project)

    if request.method == "POST":
        # collect answers defined by the type's form_schema
        form_data = {
            f["name"]: request.POST.get(f["name"], "")
            for f in doc_type.form_schema
        }
        title = request.POST.get("title") or f"{doc_type.name} - {project.client.name}"
        document = Document.objects.create(
            project=project, document_type=doc_type, title=title,
            form_data=form_data, created_by=request.user,
            template=default_template_for(doc_type),
        )
        assign_number(document)   # before drafting: the body prints the number
        content = (_generate_priced(document) if doc_type.is_priced
                   else _generate(document))
        _save_version(document, content, request.user, note="AI draft")
        log_activity(request.user, "generated", document.title)
        return redirect(document)

    return render(request, "documents/form.html", {
        "project": project, "doc_type": doc_type,
    })


@login_required
@require_perm("reports", "create")
def monthly_report_create(request, project_pk):
    """One-click monthly report: facts → Groq narrative → proof photos →
    a normal editable Document (version history, Quill, PDF export)."""
    project = get_object_or_404(
        visible_projects(Project.objects.select_related("client"), request.user),
        pk=project_pk)
    doc_type = get_object_or_404(DocumentType, slug="monthly-report", is_active=True)
    image_assets = [a for a in project.assets.all() if a.is_image]

    if request.method == "POST":
        month_str = request.POST.get("month") or timezone.localdate().strftime("%Y-%m")
        try:
            year, month = int(month_str[:4]), int(month_str[5:7])
            if not 1 <= month <= 12:
                raise ValueError
        except (ValueError, IndexError):
            messages.error(request, "Pick a valid month.")
            return redirect("documents:monthly_report", project_pk=project.pk)
        highlights = request.POST.get("highlights", "")
        selected_ids = request.POST.getlist("assets")

        content = generate_monthly_report(
            project, year, month, highlights, selected_asset_ids=selected_ids,
        )
        month_label = f"{calendar.month_name[month]} {year}"
        document = Document.objects.create(
            project=project, document_type=doc_type,
            title=f"Monthly Report – {month_label} – {project.client.name}",
            form_data={"brief": highlights, "month": month_str},
            created_by=request.user,
            template=default_template_for(doc_type),
        )
        _save_version(document, content, request.user, note="AI generated")
        log_activity(request.user, "generated monthly report", document.title)
        messages.success(request, "Report drafted — review, edit and export below.")
        return redirect(document)

    return render(request, "documents/report_form.html", {
        "project": project,
        "image_assets": image_assets,
        "default_month": timezone.localdate().strftime("%Y-%m"),
    })


@login_required
@require_perm("documents", "view")
@block_financial_doc
def document_detail(request, pk):
    document = get_object_or_404(
        Document.objects.select_related("project", "project__client", "document_type"),
        pk=pk,
    )
    needs_approver = (Document.Status.APPROVED, Document.Status.SENT)
    status_actions = [
        {"value": s, "label": STATUS_ACTION_LABELS[s]}
        for s in STATUS_FLOW.get(document.status, [])
        if s not in needs_approver or has_perm(request.user, "documents", "approve")
    ]
    context = {
        "document": document,
        "versions": document.versions.all(),
        "status_actions": status_actions,
        "files": document.files.select_related("version").order_by("-created_at"),
    }
    # A priced document swaps the free-text editor for the line-item table;
    # build_context gives that panel the same figures the body was rendered from.
    if document.document_type.is_priced:
        context["pricing"] = build_context(document)
    return render(request, "documents/detail.html", context)


@login_required
@block_financial_doc
@require_perm("documents", "edit")
def document_save(request, pk):
    document = get_object_or_404(Document, pk=pk)
    if request.method == "POST":
        _, previous = _save_version(document, request.POST.get("content_html", ""),
                                    request.user, note="Manual edit")
        log_activity(request.user, "edited", document.title)
        _warn_if_reopened(request, document, previous)
    return redirect(document)


@login_required
@block_financial_doc
@require_perm("documents", "edit")
@require_POST
def document_items_save(request, pk):
    """Save a priced document's table: rows, dates, reference and terms.

    The whole set is replaced at once and the body re-rendered from it, so one
    save is one coherent state of the invoice rather than a stream of per-cell
    edits — and one new version, which is the unit a finance document is
    actually reviewed and restored in. Deleting a row is simply not posting it.
    """
    document = get_object_or_404(
        Document.objects.select_related("project", "project__client", "document_type"),
        pk=pk)
    if not document.document_type.is_priced:
        messages.error(request, "That document type doesn't have line items.")
        return redirect(document)

    replace_line_items(document, rows_from_post(request.POST))
    form_data = dict(document.form_data or {})
    for field in ("issue_date", "due_date"):
        parsed = _parse_date(request.POST.get(field))
        form_data[field] = parsed.isoformat() if parsed else ""
    form_data["po_number"] = request.POST.get("po_number", "").strip()[:60]
    form_data["notes"] = request.POST.get("notes", "").strip()[:4000]
    # An unticked checkbox posts nothing, so its absence IS "don't charge GST".
    form_data["charge_gst"] = request.POST.get("charge_gst") == "on"
    mode = request.POST.get("gst_mode", "auto")
    form_data["gst_mode"] = mode if mode in GST_MODES else "auto"
    document.form_data = form_data
    document.save(update_fields=["form_data", "updated_at"])

    _, previous = _save_version(document, render_body(document), request.user,
                                note="Line items updated")
    log_activity(request.user, "updated line items", document.title)
    messages.success(request, "Saved — totals and the document body were rebuilt "
                              "from the table.")
    _warn_if_reopened(request, document, previous)
    return redirect(document)


@login_required
@block_financial_doc
@require_POST
def document_autosave(request, pk):
    """Debounced background save from the editor. Reuses the current version
    while it is still an autosave, so history isn't flooded with drafts."""
    if not has_perm(request.user, "documents", "edit"):
        return JsonResponse({"ok": False, "error": "role"}, status=403)
    document = get_object_or_404(Document, pk=pk)
    content = request.POST.get("content_html", "")
    current = document.current_version
    previous = None
    if current and current.note == "Autosave":
        current.content_html = content
        current.save(update_fields=["content_html", "updated_at"])
        version = current
        # Reusing a version skips _save_version, so apply the same rule here:
        # signed-off content that changes goes back to draft.
        if document.status in SIGNED_OFF:
            previous = document.get_status_display()
            document.status = Document.Status.DRAFT
            document.save(update_fields=["status", "updated_at"])
    else:
        version, previous = _save_version(document, content, request.user,
                                          note="Autosave")
    return JsonResponse({
        "ok": True,
        "version": version.version_number,
        "saved_at": timezone.localtime().strftime("%H:%M:%S"),
        # The editor shows this so a silent background save can't quietly
        # undo an approval.
        "reopened_from": previous,
    })


@login_required
@block_financial_doc
@require_POST
def document_set_status(request, pk):
    document = get_object_or_404(Document, pk=pk)
    new_status = request.POST.get("status", "")
    needs_approver = new_status in (Document.Status.APPROVED, Document.Status.SENT)
    if needs_approver and not has_perm(request.user, "documents", "approve"):
        messages.error(request, "Only Owners and Project Managers can approve or send.")
        return redirect(document)
    if new_status in STATUS_FLOW.get(document.status, []):
        document.status = new_status
        fields = ["status", "updated_at"]
        # The review deadline is set as the document enters review and cleared
        # once it's signed off — a deadline on an approved document is history,
        # and leaving it behind would keep it on the calendar forever.
        if new_status == Document.Status.REVIEW:
            document.review_due_date = _parse_date(request.POST.get("review_due_date"))
            fields.append("review_due_date")
        elif new_status in SIGNED_OFF:
            document.review_due_date = None
            fields.append("review_due_date")
        document.save(update_fields=fields)
        log_activity(request.user, f"status → {document.get_status_display()}",
                     document.title)
        messages.success(request, f"Status updated to {document.get_status_display()}.")
        _notify_status_change(request.user, document)
    else:
        messages.error(request, "That status change isn't allowed from the current state.")
    return redirect(document)


def _notify_status_change(actor, document):
    """Submitted-for-review pings approvers; approval/sent pings the creator.

    Approvers come from the RBAC engine (documents.approve), not the deprecated
    User.role CharField — which every user carried by default, so the old query
    pinged the whole company on every submission and missed real approvers.
    """
    url = document.get_absolute_url()
    label = f"“{document.title}” is now {document.get_status_display()}"
    if document.status == Document.Status.REVIEW:
        notify(users_with_perm("documents", "approve",
                               workspace=document.project.workspace_id),
               f"{label} — awaiting approval.", url, exclude=actor)
    elif document.created_by:
        notify([document.created_by], f"{label}.", url, exclude=actor)


@login_required
@require_perm("documents", "export")
@block_financial_doc
@require_POST
def document_export_pdf(request, pk):
    document = get_object_or_404(
        Document.objects.select_related("project", "project__client",
                                        "document_type", "template", "current_version"),
        pk=pk,
    )
    if not document.current_version:
        messages.error(request, "Nothing to export yet — generate content first.")
        return redirect(document)
    try:
        pdf_bytes = render_document_pdf(document)
    except Exception:
        messages.error(request, "PDF rendering failed for this content.")
        return redirect(document)
    stem = slugify(document.number or document.title) or f"document-{document.pk}"
    filename = f"{stem}-v{document.current_version.version_number}.pdf"
    generated = GeneratedFile(document=document, version=document.current_version,
                              file_type="pdf")
    generated.file.save(filename, ContentFile(pdf_bytes))
    log_activity(request.user, "exported PDF", document.title)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
@block_financial_doc
@require_perm("documents", "edit")
def document_regenerate(request, pk):
    document = get_object_or_404(
        Document.objects.select_related("project", "project__client", "document_type"),
        pk=pk,
    )
    if request.method == "POST":
        content = (_generate_priced(document) if document.document_type.is_priced
                   else _generate(document))
        _, previous = _save_version(document, content, request.user,
                                    note="AI regenerated")
        log_activity(request.user, "regenerated", document.title)
        _warn_if_reopened(request, document, previous)
    return redirect(document)


# ---------- M6: document library actions ----------

@login_required
@block_financial_doc
@require_perm("documents", "create")
@require_POST
def document_duplicate(request, pk):
    src = get_object_or_404(
        Document.objects.select_related("document_type", "project", "current_version"),
        pk=pk,
    )
    duplicate = Document.objects.create(
        project=src.project, document_type=src.document_type,
        title=f"{src.title} (copy)", form_data=src.form_data,
        template=src.template, created_by=request.user,
    )
    assign_number(duplicate)  # numbered types get a fresh number, never a reused one
    if src.document_type.is_priced:
        # Copy the rows, then re-render: the body carries the invoice number and
        # the copy has a new one, so reusing the source's HTML would ship a
        # duplicate that prints someone else's number.
        LineItem.objects.bulk_create([
            LineItem(document=duplicate, description=item.description,
                     quantity=item.quantity, unit=item.unit,
                     unit_price=item.unit_price, tax_rate=item.tax_rate,
                     order=item.order)
            for item in src.line_items.all()
        ])
        content = render_body(duplicate)
    else:
        content = src.current_version.content_html if src.current_version else ""
    _save_version(duplicate, content, request.user,
                  note=f"Duplicated from “{src.title}”")
    log_activity(request.user, "duplicated", src.title)
    messages.success(request, f"Duplicated as “{duplicate.title}”.")
    return redirect(duplicate)


@login_required
@block_financial_doc
@require_perm("documents", "archive")
@require_POST
def document_archive(request, pk):
    document = get_object_or_404(Document, pk=pk)
    document.is_archived = True
    document.save(update_fields=["is_archived", "updated_at"])
    log_activity(request.user, "archived", document.title)
    messages.success(request, f"“{document.title}” archived. It's hidden from lists "
                              "but nothing is deleted.")
    return redirect("documents:list")


@login_required
@block_financial_doc
@require_perm("documents", "restore")
@require_POST
def document_unarchive(request, pk):
    document = get_object_or_404(Document, pk=pk)
    document.is_archived = False
    document.save(update_fields=["is_archived", "updated_at"])
    log_activity(request.user, "restored", document.title)
    messages.success(request, f"“{document.title}” restored.")
    return redirect(document)


@login_required
@block_financial_doc
@require_perm("documents", "delete")
@require_POST
def document_delete(request, pk):
    document = get_object_or_404(Document, pk=pk)
    title = document.title
    document.delete()
    log_activity(request.user, "deleted", title)
    messages.success(request, f"“{title}” permanently deleted.")
    return redirect("documents:list")


@login_required
@require_POST
def document_bulk_action(request):
    """Bulk archive / restore / delete from the library's checkboxes.
    Each bulk verb maps to its own RBAC action on the documents module."""
    action = request.POST.get("action", "")
    if action in ("archive", "restore", "delete") and \
            not has_perm(request.user, "documents", action):
        messages.error(request, f"You don't have '{action}' permission on documents.")
        return redirect("documents:list")
    documents = list(visible_documents(
        Document.objects.filter(pk__in=request.POST.getlist("selected")),
        request.user))
    back = "documents:list"
    if not documents:
        messages.error(request, "No documents selected.")
        return redirect(back)
    count = len(documents)
    label = f"{count} document{'s' if count != 1 else ''}"
    if action == "archive":
        Document.objects.filter(pk__in=[d.pk for d in documents]).update(
            is_archived=True, updated_at=timezone.now())
        for d in documents:
            log_activity(request.user, "archived", d.title)
        messages.success(request, f"Archived {label}. Nothing was deleted.")
    elif action == "restore":
        Document.objects.filter(pk__in=[d.pk for d in documents]).update(
            is_archived=False, updated_at=timezone.now())
        for d in documents:
            log_activity(request.user, "restored", d.title)
        messages.success(request, f"Restored {label}.")
    elif action == "delete":
        for d in documents:
            log_activity(request.user, "deleted", d.title)
        Document.objects.filter(pk__in=[d.pk for d in documents]).delete()
        messages.success(request, f"Permanently deleted {label}.")
    else:
        messages.error(request, "Unknown bulk action.")
    return redirect(back)


def _html_to_lines(html):
    """Flatten editor HTML into comparable text lines."""
    text = re.sub(r"(?i)</(p|div|h[1-6]|li|tr|blockquote)>|<br\s*/?>", "\n", html or "")
    return [line.strip() for line in strip_tags(text).splitlines() if line.strip()]


@login_required
@require_perm("documents", "view")
@block_financial_doc
def document_compare(request, pk):
    """M6: side-by-side diff of any two versions."""
    document = get_object_or_404(
        Document.objects.select_related("project", "project__client"), pk=pk
    )
    versions = list(document.versions.all())
    if len(versions) < 2:
        messages.error(request, "Need at least two versions to compare.")
        return redirect(document)
    numbers = {v.version_number: v for v in versions}
    default_b = document.current_version or versions[0]
    default_a = versions[1] if versions[0] == default_b else versions[0]
    try:
        a = numbers[int(request.GET.get("a", default_a.version_number))]
        b = numbers[int(request.GET.get("b", default_b.version_number))]
    except (KeyError, ValueError):
        messages.error(request, "Unknown version selected.")
        return redirect(document)
    diff_table = difflib.HtmlDiff(wrapcolumn=55).make_table(
        _html_to_lines(a.content_html), _html_to_lines(b.content_html),
        f"v{a.version_number}", f"v{b.version_number}",
    )
    return render(request, "documents/compare.html", {
        "document": document, "versions": versions,
        "a": a, "b": b, "diff_table": diff_table,
    })


@login_required
@block_financial_doc
@require_perm("documents", "edit")
@require_POST
def document_restore_version(request, pk, version_number):
    """M6: make an old version current again — as a new version, keeping history."""
    document = get_object_or_404(Document, pk=pk)
    version = get_object_or_404(document.versions, version_number=version_number)
    if version == document.current_version:
        messages.error(request, f"v{version_number} is already the current version.")
        return redirect(document)
    restored, previous = _save_version(
        document, version.content_html, request.user,
        note=f"Restored from v{version.version_number}")
    log_activity(request.user, f"restored v{version.version_number}", document.title)
    messages.success(request,
                     f"v{version.version_number} restored as v{restored.version_number}.")
    _warn_if_reopened(request, document, previous)
    return redirect(document)
