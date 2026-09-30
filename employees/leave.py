"""Leave arithmetic: how many days somebody has, how many they've used, and
what the excess costs.

Every number the leave screens and the payroll run quote comes from here, so
the balance an employee reads on their own page and the deduction that lands on
their payslip can never be two different calculations.

**What a "day" is.** Calendar days would punish anyone who takes a Friday and a
Monday: four days on the calendar, two days of work missed. So a leave day is a
*working* day — a date whose weekday is in that person's `CapacityProfile`
(Mon–Fri unless somebody has been given a different pattern) — and company-wide
public holidays inside the range are removed as well, because a holiday is not
leave anybody should be spending. A half-day counts as 0.5.

**What comes out of the pool.** `LeaveRecord.ALLOWANCE_KINDS` — vacation,
casual, sick and other. Public holidays and comp off are free. Unpaid leave is
deliberately outside the pool in both directions: it never consumes allowance
and it always costs pay, which is what the word means.

**Only approved leave counts.** A pending request changes no balance and no
payslip. Otherwise anyone could spend their allowance, or reduce their own pay,
by asking.

**The year is the calendar year.** Allowance resets on 1 January. An anniversary
year would be defensible too, but it means every employee's balance rolls over
on a different date, which is a support question every week instead of one
announcement in December.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q

from resource_planner.models import DEFAULT_WORKING_DAYS, CapacityProfile, LeaveRecord

ONE_DAY = timedelta(days=1)
ZERO = Decimal("0")
HALF = Decimal("0.5")
ONE = Decimal("1")
# Indian payroll convention: a fixed 30-day divisor, so a day off costs the same
# in February as in March. Dividing by each month's actual working days is
# arguably more precise and is certainly more surprising — the per-day rate
# would move every month, and two people on identical salaries would lose
# different amounts for the same single day.
DAYS_IN_MONTH_FOR_PAY = Decimal("30")


# ---------------------------------------------------------------------------
# entitlement
# ---------------------------------------------------------------------------

def agency_default_days():
    from core.models import AgencySettings
    return AgencySettings.load().default_annual_leave_days


def allowance_for(profile):
    """This person's paid days per year. Their override, or the agency floor."""
    if profile is not None and profile.annual_leave_days is not None:
        return profile.annual_leave_days
    return agency_default_days()


# ---------------------------------------------------------------------------
# counting days
# ---------------------------------------------------------------------------

def working_weekdays(user):
    """ISO weekday digits this person works, e.g. "12345"."""
    profile = CapacityProfile.objects.filter(user=user).first()
    return (profile.working_days if profile else DEFAULT_WORKING_DAYS) \
        or DEFAULT_WORKING_DAYS


def holiday_dates(start, end, workspace_id=None):
    """Company-wide non-working dates in the range, as a set.

    Company holidays are `LeaveRecord`s with no user (see that model), so this
    reads the same table the planner does rather than inventing a second
    calendar that could drift out of step with it.
    """
    records = LeaveRecord.objects.filter(
        user__isnull=True, kind=LeaveRecord.Kind.HOLIDAY,
        status=LeaveRecord.Status.APPROVED,
        start_date__lte=end, end_date__gte=start)
    if workspace_id is not None:
        records = records.filter(
            Q(workspace_id=workspace_id) | Q(workspace__isnull=True))
    dates = set()
    for record in records:
        cursor = max(record.start_date, start)
        last = min(record.end_date, end)
        while cursor <= last:
            dates.add(cursor)
            cursor += ONE_DAY
    return dates


def countable_days(record, *, within=None, weekdays=None, holidays=None):
    """How many working days `record` actually costs, as a Decimal.

    `within` narrows to a (start, end) window — the payroll run asks "how much
    of this leave fell in May", and a record spanning a month boundary must
    contribute to each month only the part that belongs to it.
    """
    start, end = record.start_date, record.end_date
    if within is not None:
        start = max(start, within[0])
        end = min(end, within[1])
        if start > end:
            return ZERO

    if weekdays is None:
        weekdays = working_weekdays(record.user)
    if holidays is None:
        holidays = holiday_dates(start, end, record.workspace_id)

    total = ZERO
    cursor = start
    while cursor <= end:
        if str(cursor.isoweekday()) in weekdays and cursor not in holidays:
            total += ONE
        cursor += ONE_DAY
    # A half-day is a half-day whatever the span. Ranges longer than one day
    # are not halved: `is_half_day` on a fortnight is data entry, not a policy
    # anyone runs, and halving it would quietly under-charge the allowance.
    if record.is_half_day and total > ZERO:
        total = HALF if total == ONE else total
    return total


def year_bounds(year):
    return date(year, 1, 1), date(year, 12, 31)


# ---------------------------------------------------------------------------
# balances
# ---------------------------------------------------------------------------

def approved_records(user, start, end, kinds=None):
    records = LeaveRecord.objects.filter(
        user=user, status=LeaveRecord.Status.APPROVED,
        start_date__lte=end, end_date__gte=start)
    if kinds is not None:
        records = records.filter(kind__in=kinds)
    return records


def days_taken(user, start, end, kinds=None):
    """Working days of approved leave in the window, in one pass.

    The weekday pattern and the holiday set are resolved once and handed to
    every record rather than looked up per record — a year of leave is a dozen
    rows, and this is called for every employee on the payroll screen.
    """
    records = list(approved_records(user, start, end, kinds))
    if not records:
        return ZERO
    weekdays = working_weekdays(user)
    workspace_id = getattr(user, "workspace_id", None)
    holidays = holiday_dates(start, end, workspace_id)
    return sum(
        (countable_days(record, within=(start, end), weekdays=weekdays,
                        holidays=holidays) for record in records),
        ZERO)


def balance(profile, year=None):
    """{allowed, used, remaining, over, unpaid, pending} for one person/year.

    `over` is the part of `used` beyond the allowance — the days that will cost
    salary. `unpaid` is separate and always costs, so the two are added when
    the payroll run works out a deduction, never confused with one another.
    """
    from django.utils import timezone

    year = year or timezone.localdate().year
    start, end = year_bounds(year)
    user = profile.user
    allowed = allowance_for(profile)

    used = days_taken(user, start, end, LeaveRecord.ALLOWANCE_KINDS)
    unpaid = days_taken(user, start, end, LeaveRecord.UNPAID_KINDS)
    pending = LeaveRecord.objects.filter(
        user=user, status=LeaveRecord.Status.REQUESTED,
        start_date__lte=end, end_date__gte=start).count()

    remaining = Decimal(allowed) - used
    return {
        "year": year,
        "allowed": allowed,
        "used": used,
        "remaining": max(remaining, ZERO),
        "over": max(-remaining, ZERO),
        "unpaid": unpaid,
        "pending": pending,
    }


# ---------------------------------------------------------------------------
# what it costs
# ---------------------------------------------------------------------------

def daily_rate(gross_monthly):
    if not gross_monthly:
        return ZERO
    return Decimal(gross_monthly) / DAYS_IN_MONTH_FOR_PAY


def month_bounds(month_start):
    """(first, last) of the month `month_start` falls in."""
    first = month_start.replace(day=1)
    if first.month == 12:
        last = date(first.year, 12, 31)
    else:
        last = date(first.year, first.month + 1, 1) - ONE_DAY
    return first, last


def deduction_for_month(profile, month_start):
    """What this month's leave costs, and the figures behind it.

    The allowance is annual but pay is monthly, so "are they over?" has to be
    asked as at the end of *this* month, not for the month in isolation. The
    year-to-date total decides how much of this month's leave is unpaid: the
    first days of the year are free until the pool runs dry, and everything
    after it is charged. That is what stops a deduction depending on whether
    somebody happened to take their leave in January or November.
    """
    first, last = month_bounds(month_start)
    user = profile.user
    allowed = Decimal(allowance_for(profile))
    year_start, _ = year_bounds(first.year)

    # Year to date, up to the end of this month, and up to the end of the one
    # before it. The difference is the part of the excess this month created.
    used_through_month = days_taken(user, year_start, last,
                                    LeaveRecord.ALLOWANCE_KINDS)
    used_before_month = (
        ZERO if first <= year_start
        else days_taken(user, year_start, first - ONE_DAY,
                        LeaveRecord.ALLOWANCE_KINDS))

    over_through = max(used_through_month - allowed, ZERO)
    over_before = max(used_before_month - allowed, ZERO)
    excess_this_month = over_through - over_before

    unpaid_this_month = days_taken(user, first, last, LeaveRecord.UNPAID_KINDS)
    chargeable = excess_this_month + unpaid_this_month
    gross = profile.salary or ZERO

    return {
        "month": first,
        "gross": Decimal(gross),
        "allowed": int(allowed),
        "taken_this_month": days_taken(user, first, last,
                                       LeaveRecord.ALLOWANCE_KINDS),
        "used_year_to_date": used_through_month,
        "excess_days": excess_this_month,
        "unpaid_days": unpaid_this_month,
        "chargeable_days": chargeable,
        "daily_rate": daily_rate(gross),
        "deduction": (daily_rate(gross) * chargeable).quantize(Decimal("0.01")),
    }
