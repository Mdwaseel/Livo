"""Birthdays, read live from `employees.EmployeeProfile.date_of_birth`.

The date of birth lives on the HR profile, where a person's other personal facts
already are — not in a calendar table. Putting it here would have meant either
duplicating it or inventing a second place to maintain it, and a birthday is not
a calendar's fact to own.

**The year never leaves this module.** A date of birth is an age, and an age is
HR-sensitive in a way a birthday is not: the whole agency can reasonably know
it's Anna's birthday on Thursday without also learning she turns 47. Every
`Event` produced below is anchored to the day and month in the window's year, and
the stored year is used for nothing.
"""
from datetime import date

from employees.models import EmployeeProfile

from ..events import Event
from .base import EventSource, register, search_filter

# People who have left don't have birthdays at work any more.
GONE = (EmployeeProfile.Status.RESIGNED, EmployeeProfile.Status.TERMINATED)


@register
class BirthdaySource(EventSource):
    key = "birthday"
    kinds = ("birthday",)
    # Gated on `employees.view`: you see the birthdays of people whose records
    # you can already see. Nothing new is exposed by the calendar.
    module = "employees"
    supports = frozenset({"employee", "department"})
    move_action = ""

    def fetch(self, query):
        """One query, whatever the window.

        Filtered in Python rather than in SQL because a birthday is a
        (month, day) recurring yearly, and expressing "any of the 31 month/day
        pairs in this window" as a WHERE clause means either 31 OR-ed
        conditions or a database-specific date function. The set being walked is
        the agency's headcount — bounded by how many people work here, not by
        how much data has accumulated — so the loop is the cheap side of that
        trade, and the query count stays at one for a day view and a year view
        alike.
        """
        queryset = (EmployeeProfile.objects
                    .filter(date_of_birth__isnull=False, user__is_active=True)
                    .exclude(status__in=GONE)
                    .select_related("user", "user__department"))
        if query.employee_id:
            queryset = queryset.filter(user_id=query.employee_id)
        if query.department_id:
            queryset = queryset.filter(user__department_id=query.department_id)
        queryset = search_filter(queryset, query.search,
                                 "user__first_name", "user__last_name",
                                 "user__username")

        for profile in queryset:
            for day in _birthdays_between(profile.date_of_birth,
                                          query.start, query.end):
                yield Event(
                    # The year is part of the key so the reminder ledger can
                    # tell this year's birthday from next year's.
                    key=f"birthday:{profile.pk}@{day.isoformat()}",
                    kind="birthday",
                    title=f"{profile.display_name}’s birthday",
                    start_date=day,
                    url=profile.get_absolute_url(),
                    detail=(profile.user.department.name
                            if profile.user.department_id else ""),
                    user_id=profile.user_id,
                    user_name=profile.display_name,
                    department_id=profile.user.department_id,
                    source=self.key,
                    object_id=profile.pk,
                    occurrence_date=day,
                    is_movable=False,
                )


def _birthdays_between(born, start, end):
    """The anniversaries of `born` that fall in [start, end].

    Iterating over the window's years rather than its days keeps this O(1) for
    any realistic window. 29 February falls back to the 28th on common years,
    which is the convention every other system uses and the alternative to a
    birthday that disappears three years in four.
    """
    for year in range(start.year, end.year + 1):
        try:
            day = born.replace(year=year)
        except ValueError:  # 29 Feb in a non-leap year
            day = date(year, 2, 28)
        if start <= day <= end:
            yield day
