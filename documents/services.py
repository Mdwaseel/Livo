"""
Server-side document business logic: auto-numbering, template resolution,
and monthly report generation.

Numbers (INV-2026-001, QUO-2026-001) are always assigned here, never by the
AI. With the single-brief input model the AI derives any money figures from
the brief itself, so the old server-side GST math is gone.
"""
import calendar

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.html import escape

from .models import Document, Template

# Document types that get a sequential number, and their prefix.
NUMBER_PREFIXES = {"invoice": "INV", "quotation": "QUO"}

# How many times to re-read the sequence when another request took our number.
NUMBER_ATTEMPTS = 10


def assign_number(document):
    """Set a yearly sequential number like INV-2026-001 on numbered types.

    Concurrency-safe by construction rather than by locking: SELECT ... FOR
    UPDATE can only lock rows that already exist, so it never stopped two
    simultaneous creations computing the same next sequence. Instead the
    unique constraint on Document.number decides, and the loser re-reads and
    retries. On SQLite (no row locking at all) this is the only thing that
    works; on PostgreSQL it is also cheaper than a table lock.
    """
    prefix = NUMBER_PREFIXES.get(document.document_type.slug)
    if not prefix or document.number:
        return document.number
    stem = f"{prefix}-{timezone.localdate().year}-"

    for _ in range(NUMBER_ATTEMPTS):
        taken = (Document.objects.filter(number__startswith=stem)
                 .values_list("number", flat=True))
        seq = max(
            (int(n.rsplit("-", 1)[-1]) for n in taken
             if n.rsplit("-", 1)[-1].isdigit()),
            default=0,
        ) + 1
        candidate = f"{stem}{seq:03d}"
        try:
            with transaction.atomic():
                document.number = candidate
                document.save(update_fields=["number", "updated_at"])
        except IntegrityError:
            document.number = ""
            continue  # someone else claimed it — recompute and try again
        return document.number

    raise RuntimeError(
        f"Could not allocate a {prefix} number after {NUMBER_ATTEMPTS} attempts.")


def default_template_for(doc_type):
    """The visual template a new document of this type should use:
    the type's default, else any of the type's, else the any-type default,
    else any template at all. None when nothing is designed yet.

    This picks ONE template, and it is only used to pre-select `Document.
    template` when a document is created. What the PDF actually renders is
    composed per page by `resolve_template` below, so a type whose own template
    supplies only a cover still gets the universal content and back pages.
    """
    return (
        doc_type.templates.filter(is_default=True).first()
        or doc_type.templates.first()
        or Template.objects.filter(document_type=None, is_default=True).first()
        or Template.objects.filter(document_type=None).first()
    )


# ---------- composing the three pages ----------

PAGE_FIELDS = ("cover_image", "content_background", "back_image")


class ResolvedTemplate:
    """The three page images a document will actually print, each taken from
    the most specific template that supplies it.

    **Why this is not just a Template.** A template is a design surface with
    three independent slots, and the natural way to use it is one template per
    document type carrying that type's cover, plus one "Content Page" and one
    "Thank You Page" with no type, meant to apply to everything. Resolving to a
    single Template made that impossible: the type's own cover-only template
    won outright and the universal content and back pages were never reached,
    so every document came out as a cover and nothing else.

    Each slot is therefore resolved on its own, in the same order of
    specificity — the document's chosen template, then its type's default, then
    its type's others, then the any-type default, then any-type others. The
    first candidate that HAS a given image supplies it.

    The content region travels with the background rather than with the
    document, because x/y/width/height are drawn against a specific background
    image; taking the region from one template and the image from another would
    put the text in the wrong place on the page.

    It exposes the same attribute names as `Template` so `pdf.py` renders it
    without knowing the difference.
    """

    def __init__(self, candidates):
        self.sources = {}
        for field in PAGE_FIELDS:
            source = next((c for c in candidates if getattr(c, field)), None)
            setattr(self, field, getattr(source, field) if source else None)
            if source is not None:
                self.sources[field] = source

        region = self.sources.get("content_background")
        if region is None:
            # No background to draw against: fall back to the most specific
            # template's region rather than to the model defaults, so a
            # background-less design still honours a hand-drawn text area.
            region = candidates[0] if candidates else None
        self.content_x = getattr(region, "content_x", 8.0)
        self.content_y = getattr(region, "content_y", 12.0)
        self.content_width = getattr(region, "content_width", 84.0)
        self.content_height = getattr(region, "content_height", 76.0)

    @property
    def has_visual_pages(self):
        return any(getattr(self, field) for field in PAGE_FIELDS)

    def __bool__(self):
        return self.has_visual_pages

    def __repr__(self):
        parts = ", ".join(
            f"{field}={self.sources[field].name!r}"
            for field in PAGE_FIELDS if field in self.sources) or "empty"
        return f"<ResolvedTemplate {parts}>"


def template_candidates(doc_type, explicit=None):
    """Every template that could supply a page, most specific first.

    Ordered `-is_default` then `name` so "the default" genuinely wins among a
    type's templates, and the rest are in a stable order rather than whatever
    the database happens to return.
    """
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    if doc_type is not None:
        candidates += list(doc_type.templates.order_by("-is_default", "name"))
    candidates += list(
        Template.objects.filter(document_type=None).order_by("-is_default", "name"))

    seen, ordered = set(), []
    for candidate in candidates:
        if candidate.pk not in seen:
            seen.add(candidate.pk)
            ordered.append(candidate)
    return ordered


def resolve_template(doc_type, explicit=None):
    """The composed page set for a document. Never None — ask
    `has_visual_pages` whether there is anything to print."""
    return ResolvedTemplate(template_candidates(doc_type, explicit))


# ---------- monthly report ----------

def _month_facts(project, year, month, highlights=""):
    """Structured plain-text facts for the report prompt: that month's work
    log grouped by category, tasks completed, and milestones marked done."""
    from projects.models import Milestone

    lines = []

    logs = project.work_logs.filter(date__year=year, date__month=month).order_by("date")
    by_category = {}
    for entry in logs:
        by_category.setdefault(entry.get_category_display(), []).append(entry)
    if by_category:
        lines.append("WORK LOG (grouped by area):")
        for category, entries in by_category.items():
            lines.append(f"{category}:")
            for e in entries:
                hours = f" [{e.hours}h]" if e.hours else ""
                lines.append(f"  - {e.date.strftime('%d %b')}: {e.description}{hours}")

    tasks = project.tasks.filter(completed_on__year=year, completed_on__month=month)
    if tasks:
        lines.append("TASKS COMPLETED:")
        for t in tasks:
            lines.append(f"  - {t.title}"
                         + (f" ({t.get_priority_display()} priority)" if t.priority else ""))

    # completed_on, not updated_at: an edit to an old milestone's wording must
    # not make it resurface as "reached this month".
    milestones = project.milestones.filter(
        status=Milestone.Status.DONE,
        completed_on__year=year, completed_on__month=month,
    )
    if milestones:
        lines.append("MILESTONES REACHED:")
        for m in milestones:
            lines.append(f"  - {m.title}")

    if not lines:
        lines.append("No work log entries, completed tasks or milestones were "
                     "recorded this month.")

    if highlights.strip():
        lines.append(f"TEAM HIGHLIGHTS NOTE: {highlights.strip()}")

    return "\n".join(lines)


def _offline_report_draft(project, month_label, facts):
    """Structured draft used when no Groq key is configured / all keys fail,
    so the report flow stays fully testable offline."""
    facts_html = "".join(
        f"<p>{escape(line)}</p>" for line in facts.splitlines() if line.strip()
    )
    return (
        f"<h1>Monthly Progress Report — {escape(month_label)}</h1>"
        f"<p><strong>{escape(project.client.name)}</strong> · "
        f"{escape(project.name)}</p>"
        "<p><em>Draft assembled offline from the project's records — set "
        "GROQ_API_KEYS in .env for an AI-written narrative.</em></p>"
        "<h2>This month's record</h2>"
        f"{facts_html}"
        "<h2>Next month</h2><p>Planned work will be shared in the next update.</p>"
    )


def generate_monthly_report(project, year, month, highlights="",
                            selected_asset_ids=None):
    """Build the monthly report HTML: Groq-written narrative from that month's
    facts, then OUR code appends the Proof & Impact image section. The AI is
    told to never emit <img> tags; images are injected here from Assets.

    selected_asset_ids=None means "use the default selection" (assets flagged
    is_report_proof). An explicit list — even an empty one — is respected.
    """
    from ai_engine.services import generate_content
    from .models import DocumentType

    month_label = f"{calendar.month_name[month]} {year}"
    facts = _month_facts(project, year, month, highlights)
    doc_type = DocumentType.objects.get(slug="monthly-report")

    html = generate_content(
        doc_type,
        {"brief": highlights},
        project.client,
        project,
        extra_context={"facts": facts, "month_label": month_label},
        fallback_html=_offline_report_draft(project, month_label, facts),
    )

    assets = project.assets.all()
    if selected_asset_ids is None:
        assets = assets.filter(is_report_proof=True)
    else:
        assets = assets.filter(pk__in=selected_asset_ids)
    images = [a for a in assets if a.is_image]
    if images:
        figures = "".join(
            f'<p style="text-align:center"><img src="{a.file.url}" '
            f'style="max-width:100%" alt="{escape(a.title)}"><br>'
            f"<em>{escape(a.title)}</em></p>"
            for a in images
        )
        html += f"<h2>Proof &amp; Impact</h2>{figures}"

    return html
