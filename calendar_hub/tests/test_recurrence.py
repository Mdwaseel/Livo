"""Recurrence expansion — pure arithmetic, so tested without the database
wherever the model can be built unsaved.

The cases here are the ones that actually break RRULE implementations: the 31st
in February, 29 February in a common year, a multi-weekday weekly rule whose
first week starts mid-week, COUNT meaning "since the series began" rather than
"since the window", and an exception that moves an instance *into* a window it
would not otherwise appear in.
"""
from datetime import date, timedelta

from django.test import SimpleTestCase, TestCase

from calendar_hub import recurrence
from calendar_hub.models import MAX_OCCURRENCES, CalendarEvent, EventOccurrence

from .factories import MONDAY, Scenario


def series(**kwargs):
    """An unsaved event. Expansion never touches the database."""
    kwargs.setdefault("title", "Series")
    kwargs.setdefault("interval", 1)
    return CalendarEvent(**kwargs)


def dates(event, start, end, overrides=None):
    return [effective for _, effective, _ in
            recurrence.expand(event, start, end, overrides=overrides)]


class NonRecurringTests(SimpleTestCase):
    def test_a_single_event_produces_its_own_date(self):
        event = series(start_date=MONDAY, frequency=CalendarEvent.Frequency.NONE)
        self.assertEqual(dates(event, MONDAY, MONDAY + timedelta(days=7)),
                         [MONDAY])

    def test_a_single_event_outside_the_window_produces_nothing(self):
        event = series(start_date=MONDAY, frequency=CalendarEvent.Frequency.NONE)
        self.assertEqual(dates(event, MONDAY + timedelta(days=1),
                               MONDAY + timedelta(days=7)), [])

    def test_a_multi_day_event_is_found_by_a_window_over_its_middle(self):
        """The window test is an overlap, not a containment: a fortnight-long
        event has to be found by a window that only touches its middle."""
        event = series(start_date=MONDAY, end_date=MONDAY + timedelta(days=13),
                       frequency=CalendarEvent.Frequency.NONE)
        found = recurrence.expand(event, MONDAY + timedelta(days=5),
                                  MONDAY + timedelta(days=6))
        self.assertEqual([effective for _, effective, _ in found], [MONDAY])


class DailyTests(SimpleTestCase):
    def test_every_day(self):
        event = series(start_date=MONDAY,
                       frequency=CalendarEvent.Frequency.DAILY)
        self.assertEqual(len(dates(event, MONDAY, MONDAY + timedelta(days=6))), 7)

    def test_every_other_day(self):
        event = series(start_date=MONDAY, interval=2,
                       frequency=CalendarEvent.Frequency.DAILY)
        self.assertEqual(
            dates(event, MONDAY, MONDAY + timedelta(days=4)),
            [MONDAY, MONDAY + timedelta(days=2), MONDAY + timedelta(days=4)])

    def test_a_window_after_the_start_still_lands_on_the_rules_days(self):
        """Expansion runs from the series start, not from the window, so an
        every-other-day rule keeps its parity three weeks later.

        Expanding from the window instead would put the first occurrence on
        whatever day the window happened to open on — the series would silently
        re-phase every time somebody paged forward.
        """
        event = series(start_date=MONDAY, interval=2,
                       frequency=CalendarEvent.Frequency.DAILY)
        window_start = MONDAY + timedelta(days=21)
        found = dates(event, window_start, window_start + timedelta(days=4))
        self.assertTrue(all((day - MONDAY).days % 2 == 0 for day in found))
        self.assertEqual(found[0], MONDAY + timedelta(days=22))


class WeeklyTests(SimpleTestCase):
    def test_bare_weekly_uses_the_start_dates_own_weekday(self):
        event = series(start_date=MONDAY,
                       frequency=CalendarEvent.Frequency.WEEKLY)
        self.assertEqual(dates(event, MONDAY, MONDAY + timedelta(days=21)),
                         [MONDAY, MONDAY + timedelta(days=7),
                          MONDAY + timedelta(days=14), MONDAY + timedelta(days=21)])

    def test_multiple_weekdays(self):
        event = series(start_date=MONDAY, weekdays="14",  # Mon + Thu
                       frequency=CalendarEvent.Frequency.WEEKLY)
        self.assertEqual(
            dates(event, MONDAY, MONDAY + timedelta(days=7)),
            [MONDAY, MONDAY + timedelta(days=3), MONDAY + timedelta(days=7)])

    def test_a_weekday_before_the_start_date_is_not_produced(self):
        """A "Mon and Thu" series starting on a Thursday must not back-fill the
        Monday of that same week."""
        thursday = MONDAY + timedelta(days=3)
        event = series(start_date=thursday, weekdays="14",
                       frequency=CalendarEvent.Frequency.WEEKLY)
        found = dates(event, MONDAY, MONDAY + timedelta(days=7))
        self.assertNotIn(MONDAY, found)
        self.assertEqual(found[0], thursday)

    def test_fortnightly_skips_the_intervening_week(self):
        event = series(start_date=MONDAY, interval=2,
                       frequency=CalendarEvent.Frequency.WEEKLY)
        found = dates(event, MONDAY, MONDAY + timedelta(days=28))
        self.assertEqual(found, [MONDAY, MONDAY + timedelta(days=14),
                                 MONDAY + timedelta(days=28)])


class MonthlyTests(SimpleTestCase):
    def test_same_day_each_month(self):
        event = series(start_date=date(2026, 1, 15),
                       frequency=CalendarEvent.Frequency.MONTHLY)
        self.assertEqual(dates(event, date(2026, 1, 1), date(2026, 4, 30)),
                         [date(2026, 1, 15), date(2026, 2, 15),
                          date(2026, 3, 15), date(2026, 4, 15)])

    def test_the_31st_clamps_to_february_then_recovers(self):
        """The trap `RecurringTask._step` documents: clamping against last
        month's clamped result makes a 31st series stick at the 28th forever.
        Clamping against the anchor is what makes March the 31st again."""
        event = series(start_date=date(2026, 1, 31),
                       frequency=CalendarEvent.Frequency.MONTHLY)
        found = dates(event, date(2026, 1, 1), date(2026, 4, 30))
        self.assertEqual(found, [date(2026, 1, 31), date(2026, 2, 28),
                                 date(2026, 3, 31), date(2026, 4, 30)])

    def test_crossing_a_year_boundary(self):
        event = series(start_date=date(2026, 11, 10),
                       frequency=CalendarEvent.Frequency.MONTHLY)
        self.assertEqual(dates(event, date(2026, 11, 1), date(2027, 1, 31)),
                         [date(2026, 11, 10), date(2026, 12, 10),
                          date(2027, 1, 10)])


class YearlyTests(SimpleTestCase):
    def test_same_date_each_year(self):
        event = series(start_date=date(2026, 3, 4),
                       frequency=CalendarEvent.Frequency.YEARLY)
        self.assertEqual(dates(event, date(2026, 1, 1), date(2028, 12, 31)),
                         [date(2026, 3, 4), date(2027, 3, 4), date(2028, 3, 4)])

    def test_29_february_falls_back_to_the_28th(self):
        """The alternative is an anniversary that vanishes three years in four."""
        event = series(start_date=date(2024, 2, 29),
                       frequency=CalendarEvent.Frequency.YEARLY)
        self.assertEqual(dates(event, date(2024, 1, 1), date(2026, 12, 31)),
                         [date(2024, 2, 29), date(2025, 2, 28), date(2026, 2, 28)])


class LimitTests(SimpleTestCase):
    def test_until_stops_the_series(self):
        event = series(start_date=MONDAY,
                       frequency=CalendarEvent.Frequency.WEEKLY,
                       repeat_until=MONDAY + timedelta(days=14))
        self.assertEqual(len(dates(event, MONDAY, MONDAY + timedelta(days=60))), 3)

    def test_count_is_measured_from_the_series_start_not_the_window(self):
        """"The 10th occurrence" means the 10th since the series began. Counting
        from the window would make a series that ended in March reappear in
        June."""
        event = series(start_date=MONDAY,
                       frequency=CalendarEvent.Frequency.DAILY, repeat_count=5)
        # Days 0-4 exist; the window starting on day 6 must be empty.
        self.assertEqual(dates(event, MONDAY, MONDAY + timedelta(days=4)),
                         [MONDAY + timedelta(days=n) for n in range(5)])
        self.assertEqual(dates(event, MONDAY + timedelta(days=6),
                               MONDAY + timedelta(days=30)), [])

    def test_an_endless_daily_series_is_capped(self):
        """A rule with no end is legitimate, but something has to stop the
        expansion before it builds a hundred thousand objects."""
        event = series(start_date=MONDAY,
                       frequency=CalendarEvent.Frequency.DAILY)
        found = dates(event, MONDAY, MONDAY + timedelta(days=5000))
        self.assertEqual(len(found), MAX_OCCURRENCES)

    def test_a_repeat_count_above_the_cap_is_still_capped(self):
        event = series(start_date=MONDAY,
                       frequency=CalendarEvent.Frequency.DAILY,
                       repeat_count=MAX_OCCURRENCES + 500)
        self.assertLessEqual(
            len(dates(event, MONDAY, MONDAY + timedelta(days=5000))),
            MAX_OCCURRENCES)


class OverrideTests(TestCase):
    """Exceptions to a series. These need the database — an override is a row."""

    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_cancelled_occurrence_disappears(self):
        EventOccurrence.objects.create(
            event=self.data.standup, original_date=MONDAY + timedelta(days=7),
            is_cancelled=True)
        overrides = {o.original_date: o for o in
                     EventOccurrence.objects.filter(event=self.data.standup)}
        found = dates(self.data.standup, MONDAY, MONDAY + timedelta(days=14),
                      overrides)
        self.assertEqual(found, [MONDAY, MONDAY + timedelta(days=14)])

    def test_a_moved_occurrence_appears_on_its_new_date(self):
        EventOccurrence.objects.create(
            event=self.data.standup, original_date=MONDAY,
            moved_to=MONDAY + timedelta(days=2))
        overrides = {o.original_date: o for o in
                     EventOccurrence.objects.filter(event=self.data.standup)}
        found = dates(self.data.standup, MONDAY, MONDAY + timedelta(days=6),
                      overrides)
        self.assertEqual(found, [MONDAY + timedelta(days=2)])

    def test_an_occurrence_can_be_moved_into_a_window_it_would_have_missed(self):
        """The override has to be resolved before the window is checked, or an
        instance dragged forward from last week silently vanishes."""
        EventOccurrence.objects.create(
            event=self.data.standup, original_date=MONDAY,
            moved_to=MONDAY + timedelta(days=3))
        overrides = {o.original_date: o for o in
                     EventOccurrence.objects.filter(event=self.data.standup)}
        window_start = MONDAY + timedelta(days=2)
        found = dates(self.data.standup, window_start,
                      window_start + timedelta(days=2), overrides)
        self.assertEqual(found, [MONDAY + timedelta(days=3)])

    def test_the_original_date_stays_the_instances_identity(self):
        """RECURRENCE-ID never changes. Without that, a moved instance can't be
        found again to be moved back."""
        EventOccurrence.objects.create(
            event=self.data.standup, original_date=MONDAY,
            moved_to=MONDAY + timedelta(days=2))
        overrides = {o.original_date: o for o in
                     EventOccurrence.objects.filter(event=self.data.standup)}
        occurrence_date, effective, _ = recurrence.expand(
            self.data.standup, MONDAY, MONDAY + timedelta(days=6),
            overrides=overrides)[0]
        self.assertEqual(occurrence_date, MONDAY)
        self.assertEqual(effective, MONDAY + timedelta(days=2))


class KeyTests(SimpleTestCase):
    def test_an_occurrence_key_names_the_instance_not_the_series(self):
        """Two occurrences of the same stand-up must have different keys, or
        reminding about one silences the other forever."""
        first = recurrence.occurrence_key(7, MONDAY)
        second = recurrence.occurrence_key(7, MONDAY + timedelta(days=7))
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("event:7@"))
