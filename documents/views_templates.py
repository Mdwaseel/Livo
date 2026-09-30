"""
Template designer (Admin → Templates, mounted at /templates/).

A template is designed visually: upload the page images you want and click-drag
one rectangular content region over the content page. The region arrives as
percentages of the A4 preview and is stored as-is, so it is resolution-
independent at PDF-export time.

All three page images are optional and independent — pdf.py skips whichever is
missing — so a "content page only" template that stays constant across every
document is a valid design, not a half-finished one. Nothing here is mandatory
but the name; a template with no image at all simply has no visual pages and
falls back to the branded shell.
Everyone can browse; writes are gated to the settings module (Owner).
"""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.permissions import require_perm
from core.tenancy import home_user_required
from core.models import log_activity
from core.uploads import process_upload, uncapped_uploads
from .services import resolve_template
from .models import DocumentType, Template

# Fallback region (matches the model defaults) when the posted values are junk.
DEFAULT_REGION = {"x": 8.0, "y": 12.0, "width": 84.0, "height": 76.0}

PAGE_FIELDS = ("cover_image", "content_background", "back_image")


def _clamp(raw, fallback, lo=0.0, hi=100.0):
    try:
        return max(lo, min(hi, float(raw)))
    except (TypeError, ValueError):
        return fallback


def _region_from_post(post, current):
    """Sanitise the drawn rectangle: keep every edge on the page and never
    save a degenerate (invisible) region."""
    x = _clamp(post.get("content_x"), current["x"], hi=99.0)
    y = _clamp(post.get("content_y"), current["y"], hi=99.0)
    width = _clamp(post.get("content_width"), current["width"], lo=1.0)
    height = _clamp(post.get("content_height"), current["height"], lo=1.0)
    width = max(1.0, min(width, 100.0 - x))
    height = max(1.0, min(height, 100.0 - y))
    return {"x": round(x, 2), "y": round(y, 2),
            "width": round(width, 2), "height": round(height, 2)}


@login_required
def template_list(request):
    # Read-only for everyone who can reach it, including partner workspaces:
    # templates are shared, so a partner's invoices are generated from the same
    # letterhead ours are. Writing to them is @home_user_required below —
    # editing a shared template silently changes OUR documents too, which is
    # not something a partner's own-world autonomy should extend to.
    templates = Template.objects.select_related("document_type")
    return render(request, "documents/template_list.html", {
        "templates": templates,
    })


# `uncapped_uploads` is OUTERMOST on purpose: it has to replace the upload
# handlers while the request body is still unread, and every decorator below it
# only reads request.user. It re-applies the CSRF check itself — see the
# docstring in core.uploads, which is where the reasoning lives.
@uncapped_uploads
@login_required
@home_user_required
@require_perm("templates", "create")
def template_create(request):
    return _template_form(request, None)


@uncapped_uploads
@login_required
@home_user_required
@require_perm("templates", "edit")
def template_edit(request, pk):
    template = get_object_or_404(Template, pk=pk)
    return _template_form(request, template)


def _apply_page_images(request, template):
    """Upload, keep or clear each of the three page images.

    Returns an error message, or None when every field settled cleanly. A
    cleared image has its file removed from disk too: the media directory is
    the VPS's disk, and nothing else will ever come back for an orphan.

    These three are the app's one exception to the upload rules. They are
    full-bleed A4 print designs that every future document is generated on, not
    attachments somebody is filing — so the size ceiling and the 2000px
    downscale are both lifted here and nowhere else. A letterhead quietly
    resampled on the way in is a loss nobody notices until a client is looking
    at the PDF.

    Re-encoding is off by default too, and lifting the pixel cap alone was not
    enough to fix this. `compress_image` converts an opaque PNG to JPEG at
    IMAGE_QUALITY (80) whatever its dimensions, so a 24.9 MB print master came
    back out at 5.1 MB of quality-80 JPEG with the right dimensions and visible
    banding across every gradient. Correct for a phone photo in a comment
    thread; wrong for the letterhead under every invoice this agency sends.

    Setting TEMPLATE_IMAGE_MAX_EDGE to a real pixel value turns re-encoding
    back on with that cap, for anyone who would rather have small files than
    exact ones.

    The extension allow-list still applies: `process_upload` runs it on every
    path regardless of what these arguments say.
    """
    # 0 (the default) means "store exactly what was uploaded".
    max_edge = settings.TEMPLATE_IMAGE_MAX_EDGE
    for field in PAGE_FIELDS:
        upload = request.FILES.get(field)
        if upload:
            try:
                setattr(template, field, process_upload(
                    upload,
                    compress=bool(max_edge),
                    max_bytes=settings.TEMPLATE_UPLOAD_MAX_BYTES,
                    max_edge=max_edge))
            except ValidationError as exc:
                return exc.messages[0]
        elif request.POST.get(f"remove_{field}") == "on":
            existing = getattr(template, field)
            if existing:
                existing.delete(save=False)
            setattr(template, field, None)
    return None


PAGE_LABELS = {
    "cover_image": "Cover",
    "content_background": "Content background",
    "back_image": "Last page",
}


def _inherited_pages(template):
    """{label: source name} for the pages this template leaves empty.

    Answers the question the designer actually has in front of this form: if I
    do not upload a cover here, what prints? Without it an inherited page and a
    defined one look identical, and the first instinct is to upload a duplicate
    of something that was already applying.
    """
    if template is None or template.pk is None:
        return {}
    composed = resolve_template(template.document_type, explicit=template)
    return {
        PAGE_LABELS[field]: source.name
        for field, source in composed.sources.items()
        if not getattr(template, field) and source.pk != template.pk
    }


def _template_form(request, template):
    if request.method == "POST":
        is_new = template is None
        if is_new:
            template = Template()
        template.name = request.POST.get("name", "").strip() or "Untitled template"
        type_id = request.POST.get("document_type", "")
        template.document_type = (
            DocumentType.objects.filter(pk=type_id).first() if type_id else None
        )
        current = {"x": template.content_x, "y": template.content_y,
                   "width": template.content_width, "height": template.content_height}
        region = _region_from_post(request.POST, current if not is_new else DEFAULT_REGION)
        template.content_x = region["x"]
        template.content_y = region["y"]
        template.content_width = region["width"]
        template.content_height = region["height"]
        error = _apply_page_images(request, template)
        if error:
            messages.error(request, error)
            return redirect(request.path)
        template.is_default = request.POST.get("is_default") == "on"
        template.save()
        if template.is_default:
            # one default per document type (including the "any type" bucket)
            Template.objects.filter(document_type=template.document_type) \
                .exclude(pk=template.pk).update(is_default=False)
        log_activity(request.user, "saved template", template.name)
        messages.success(request, f"Template “{template.name}” saved.")
        return redirect("doc_templates:list")

    region = (
        {"x": template.content_x, "y": template.content_y,
         "width": template.content_width, "height": template.content_height}
        if template else DEFAULT_REGION
    )
    return render(request, "documents/template_form.html", {
        # The browser-side guard in base.html reads this off each file input.
        # It comes from the same setting the server enforces, so the two can
        # never disagree about what is too big -- a page that refuses an upload
        # the server would have accepted is the worst of both.
        "template_upload_max_bytes": settings.TEMPLATE_UPLOAD_MAX_BYTES,
        "inherited": _inherited_pages(template),
        "template": template,
        "region": region,
        "doc_types": DocumentType.objects.filter(is_active=True),
    })


@login_required
@home_user_required
@require_perm("templates", "delete")
@require_POST
def template_delete(request, pk):
    template = get_object_or_404(Template, pk=pk)
    name = template.name
    template.delete()
    log_activity(request.user, "deleted template", name)
    messages.success(request, f"Template “{name}” deleted.")
    return redirect("doc_templates:list")
