"""Document review deadlines, read live from `documents.Document`.

`review_due_date` was added to `Document` rather than to a calendar table: when
a proposal has to be reviewed by is a fact about the proposal, and the document
page is where it is set and cleared.

Financial document types stay hidden from anyone who can't already see them.
`documents.access.hidden_doc_slugs` is the same exclusion list the document list
and detail views use, reused rather than reimplemented — a calendar that leaks
"Invoice INV-0042 review due Friday" to someone barred from the invoices module
has defeated the gate rather than passed it.
"""
from django.utils import timezone

from core.models import log_activity
from core.tenancy import scope
from documents.access import hidden_doc_slugs
from documents.models import Document
from projects.access import visible_projects
from projects.models import Project

from ..events import Event
from .base import EventSource, register, search_filter

# A review deadline on a signed-off document is history, not a task. They stay
# visible (so last month still reads correctly) but never count as overdue.
SETTLED = (Document.Status.APPROVED, Document.Status.SENT)


@register
class DocumentReviewSource(EventSource):
    key = "document"
    kinds = ("document_review",)
    module = "documents"
    supports = frozenset({"project", "client"})
    move_action = "edit"
    move_noun = "document reviews"

    def fetch(self, query):
        queryset = (Document.objects
                    .filter(review_due_date__gte=query.start,
                            review_due_date__lte=query.end,
                            is_archived=False)
                    .exclude(document_type__slug__in=hidden_doc_slugs(query.viewer))
                    .select_related("project", "project__client", "document_type")
                    .order_by("review_due_date", "id"))
        if query.project_id:
            queryset = queryset.filter(project_id=query.project_id)
        if query.client_id:
            queryset = queryset.filter(project__client_id=query.client_id)
        queryset = search_filter(queryset, query.search, "title", "number",
                                 "project__name")
        queryset = queryset.filter(
            project__in=visible_projects(Project.objects.all(), query.viewer))

        today = timezone.localdate()
        for document in queryset:
            settled = document.status in SETTLED
            yield Event(
                key=f"document:{document.pk}",
                kind="document_review",
                title=f"Review: {document.title}",
                start_date=document.review_due_date,
                url=document.get_absolute_url(),
                detail=f"{document.document_type.name} · {document.project.name}",
                status_label=document.get_status_display(),
                is_overdue=not settled and document.review_due_date < today,
                project_id=document.project_id,
                project_name=document.project.name,
                client_id=document.project.client_id,
                client_name=document.project.client.name,
                source=self.key,
                object_id=document.pk,
                is_movable=True,
            )

    def move(self, user, object_id, new_date, *, occurrence_date=None):
        self._require_move(user)
        # Workspace scope, not just the module permission: `can_move` proved
        # the viewer may reschedule things, never that this thing is theirs.
        document = (scope(Document.objects, user, path="project__workspace")
                    .filter(pk=object_id, is_archived=False)
                    .select_related("document_type", "project").first())
        if document is None:
            raise LookupError("That document no longer exists.")
        # The visibility gate again: `can_move` only proved the viewer may edit
        # documents in general, not that they may see this financial one.
        if document.document_type.slug in hidden_doc_slugs(user):
            raise LookupError("That document no longer exists.")
        previous = document.review_due_date
        if previous == new_date:
            return document, ""
        document.review_due_date = new_date
        document.save(update_fields=["review_due_date", "updated_at"])
        log_activity(user, "rescheduled document review", document.title,
                     f"{previous or 'no date'} → {new_date}")
        return document, f"“{document.title}” review moved to {new_date:%d %b}."
