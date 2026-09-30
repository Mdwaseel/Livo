"""Employee leave and company holidays, read live from
`resource_planner.LeaveRecord`.

No leave data is stored here. The resource planner already owns when people are
away — it is what its capacity arithmetic runs on — and a second copy for
display would be the first thing to disagree with the planner's numbers.
"""
from resource_planner.models import LeaveRecord

from ..events import Event
from .base import EventSource, register, search_filter


@register
class LeaveSource(EventSource):
    key = "leave"
    kinds = ("leave", "holiday")
    module = "leaves"
    supports = frozenset({"employee", "department"})
    # Deliberately read-only. A leave record is a range with two ends, so a drag
    # is ambiguous — did the whole holiday move, or did it get a day longer? The
    # leave screen asks that question properly with two date fields.
    move_action = ""

    def fetch(self, query):
        queryset = (LeaveRecord.objects
                    .filter(start_date__lte=query.end, end_date__gte=query.start)
                    .exclude(status__in=(LeaveRecord.Status.REJECTED,
                                         LeaveRecord.Status.CANCELLED))
                    .select_related("user", "user__department")
                    .order_by("start_date", "id"))
        if query.employee_id:
            # A company-wide holiday applies to the person being filtered on as
            # much as to anybody else, so it survives the filter — the same rule
            # `resource_planner.selectors.leave_records` follows.
            from django.db.models import Q
            queryset = queryset.filter(
                Q(user_id=query.employee_id) | Q(user__isnull=True))
        if query.department_id:
            from django.db.models import Q
            queryset = queryset.filter(
                Q(user__department_id=query.department_id) | Q(user__isnull=True))
        queryset = search_filter(queryset, query.search, "note",
                                 "user__first_name", "user__last_name")

        for record in queryset:
            company_wide = record.user_id is None
            kind = ("holiday" if company_wide
                    or record.kind == LeaveRecord.Kind.HOLIDAY else "leave")
            if not query.wants(kind):
                continue
            who = _person(record.user) if record.user else "Everyone"
            yield Event(
                key=f"leave:{record.pk}",
                kind=kind,
                title=(record.note or record.get_kind_display() if company_wide
                       else f"{who} — {record.get_kind_display().lower()}"),
                start_date=record.start_date,
                end_date=record.end_date,
                url="/planning/leave/",
                detail=record.note if not company_wide else "",
                # Requested leave is shown but marked, matching the planner:
                # it is a warning about somebody's week, not yet a fact about it.
                status_label=("" if record.status == LeaveRecord.Status.APPROVED
                              else record.get_status_display()),
                user_id=record.user_id,
                user_name=who,
                department_id=(record.user.department_id if record.user else None),
                source=self.key,
                object_id=record.pk,
                is_movable=False,
            )


def _person(user):
    return user.get_full_name() or user.get_username()
