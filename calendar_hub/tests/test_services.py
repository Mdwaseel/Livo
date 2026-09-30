"""Collection and layout — the window arithmetic, the four grids, and the
merge that turns seven sources into one sorted list.
"""
from datetime import date, time, timedelta

from django.test import SimpleTestCase, TestCase

from calendar_hub import services
from calendar_hub.events import KIND_ORDER, CalendarQuery, Event
from calendar_hub.models import CalendarEvent

from .factories import (FRIDAY, GRID_END, GRID_START, JULY, MONDAY, MONTH_END,
                        THURSDAY, TUESDAY, WEDNESDAY, Scenario, forget_perms,
                        set_perm)


def query(viewer, window=None, **kwargs):
    if window is not None:
        kwargs.setdefault("start", window.grid_start)
        kwargs.setdefault("end", window.grid_end)
    kwargs.setdefault("start", GRID_START)
    kwargs.setdefault("end", GRID_END)
    return CalendarQuery(viewer=viewer, **kwargs)


class WindowTests(SimpleTestCase):
    def test_a_month_window_covers_the_month(self):
        window = services.Window(anchor=date(2026, 7, 17), mode="month")
        self.assertEqual((window.start, window.end), (JULY, MONTH_END))

    def test_a_month_grid_spills_into_both_neighbours(self):
        """July 2026 starts on a Wednesday, so the first row has to reach back
        into June. Fetching only the month would leave those cells blank, which
        reads as "nothing happened" rather than "not asked"."""
        window = services.Window(anchor=JULY, mode="month")
        self.assertEqual(window.grid_start, GRID_START)
        self.assertEqual(window.grid_end, GRID_END)
        self.assertEqual((window.grid_end - window.grid_start).days + 1, 35)

    def test_a_week_window_starts_on_monday(self):
        window = services.Window(anchor=WEDNESDAY, mode="week")
        self.assertEqual(window.start, MONDAY)
        self.assertEqual(window.end, MONDAY + timedelta(days=6))

    def test_a_day_window_is_one_day(self):
        window = services.Window(anchor=WEDNESDAY, mode="day")
        self.assertEqual(window.start, window.end, WEDNESDAY)

    def test_an_agenda_window_is_a_fortnight(self):
        window = services.Window(anchor=MONDAY, mode="agenda")
        self.assertEqual((window.end - window.start).days + 1,
                         services.AGENDA_PAGE_DAYS)

    def test_stepping_a_month_backwards_lands_on_the_first(self):
        window = services.Window(anchor=date(2026, 7, 17), mode="month")
        self.assertEqual(window.previous, date(2026, 6, 1))
        self.assertEqual(window.next, date(2026, 8, 1))

    def test_stepping_a_month_across_a_year_boundary(self):
        window = services.Window(anchor=date(2026, 1, 10), mode="month")
        self.assertEqual(window.previous, date(2025, 12, 1))
        window = services.Window(anchor=date(2026, 12, 10), mode="month")
        self.assertEqual(window.next, date(2027, 1, 1))

    def test_stepping_a_week(self):
        window = services.Window(anchor=MONDAY, mode="week")
        self.assertEqual(window.previous, MONDAY - timedelta(days=7))
        self.assertEqual(window.next, MONDAY + timedelta(days=7))

    def test_titles_read_naturally_in_each_mode(self):
        self.assertEqual(services.Window(JULY, "month").title, "July 2026")
        self.assertEqual(services.Window(WEDNESDAY, "day").title,
                         "Wednesday 08 July 2026")
        self.assertEqual(services.Window(MONDAY, "week").title,
                         "06 – 12 Jul 2026")

    def test_a_title_spanning_two_months_names_both(self):
        window = services.Window(anchor=date(2026, 7, 27), mode="week")
        self.assertEqual(window.title, "27 Jul – 02 Aug 2026")

    def test_a_title_spanning_two_years_names_both(self):
        window = services.Window(anchor=date(2026, 12, 28), mode="week")
        self.assertEqual(window.title, "28 Dec 2026 – 03 Jan 2027")


class CollectionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_every_kind_reaches_the_calendar(self):
        """The fixture puts one of each in the window. If a source stops
        contributing, this is what notices."""
        events = services.collect(query(self.data.manager))
        kinds = {event.kind for event in events}
        self.assertEqual(kinds, set(KIND_ORDER))

    def test_events_come_back_in_date_order(self):
        events = services.collect(query(self.data.manager))
        self.assertEqual([e.start_date for e in events],
                         sorted(e.start_date for e in events))

    def test_timed_events_sort_before_all_day_ones_on_the_same_day(self):
        """A day column that leads with "Leave" and buries the 9am stand-up
        underneath it is answering a question nobody asked."""
        events = services.collect(query(self.data.manager))
        monday = sorted([e for e in events if e.start_date == MONDAY],
                        key=lambda e: e.sort_key)
        self.assertFalse(monday[0].is_all_day)

    def test_a_viewer_without_a_module_never_sees_its_entries(self):
        kinds = {e.kind for e in services.collect(query(self.data.sales))}
        self.assertNotIn("task", kinds)
        self.assertNotIn("leave", kinds)

    def test_movability_is_resolved_against_the_viewer(self):
        """`is_movable` is asked once per source, not once per chip — a month
        grid can carry two hundred of them."""
        events = {e.key: e for e in services.collect(query(self.data.manager))}
        self.assertTrue(events[f"task:{self.data.task.pk}"].is_movable)
        # Nothing makes leave or a birthday draggable, for anybody.
        undraggable = [e for e in events.values()
                       if e.kind in ("leave", "holiday", "birthday")]
        self.assertTrue(undraggable)
        self.assertFalse(any(e.is_movable for e in undraggable))

    def test_losing_the_owning_modules_edit_right_makes_chips_undraggable(self):
        """The permission checked is the *task* module's, not the calendar's —
        so revoking it in the matrix has to reach the calendar's chips."""
        set_perm("Developer", "tasks", "edit", False)
        forget_perms(self.data.anna)
        events = services.collect(query(self.data.anna))
        tasks = [e for e in events if e.kind == "task"]
        self.assertTrue(tasks)
        self.assertFalse(any(e.is_movable for e in tasks))

    def test_the_category_filter_narrows_to_one_kind(self):
        events = services.collect(
            query(self.data.manager, kinds=frozenset({"birthday"})))
        self.assertEqual({e.kind for e in events}, {"birthday"})

    def test_search_reaches_across_sources(self):
        events = services.collect(query(self.data.manager, search="Website"))
        self.assertGreaterEqual(len({e.kind for e in events}), 2)

    def test_counts_by_kind_keeps_every_kind_in_legend_order(self):
        """Kinds with nothing in the window keep a zero row, so the legend
        doesn't reshuffle every time somebody changes month."""
        rows = services.counts_by_kind([])
        self.assertEqual([row["kind"].key for row in rows], KIND_ORDER)
        self.assertTrue(all(row["count"] == 0 for row in rows))


class BucketingTests(SimpleTestCase):
    def test_a_multi_day_event_lands_on_every_day_it_covers(self):
        event = Event(key="x", kind="leave", title="Away",
                      start_date=MONDAY, end_date=WEDNESDAY)
        buckets = services.bucket_by_day([event], MONDAY, FRIDAY)
        self.assertEqual([day for day, items in buckets.items() if items],
                         [MONDAY, TUESDAY, WEDNESDAY])

    def test_a_span_is_clipped_to_the_window(self):
        """A fortnight of leave must not render fourteen chips into a week view
        that only has room for five of them."""
        event = Event(key="x", kind="leave", title="Away",
                      start_date=MONDAY - timedelta(days=5),
                      end_date=MONDAY + timedelta(days=5))
        buckets = services.bucket_by_day([event], MONDAY, WEDNESDAY)
        self.assertEqual(sum(len(items) for items in buckets.values()), 3)

    def test_empty_days_are_present_and_empty(self):
        buckets = services.bucket_by_day([], MONDAY, FRIDAY)
        self.assertEqual(len(buckets), 5)
        self.assertTrue(all(items == [] for items in buckets.values()))

    def test_each_day_of_a_span_gets_its_own_key(self):
        event = Event(key="leave:1", kind="leave", title="Away",
                      start_date=MONDAY, end_date=TUESDAY)
        buckets = services.bucket_by_day([event], MONDAY, TUESDAY)
        self.assertNotEqual(buckets[MONDAY][0].key, buckets[TUESDAY][0].key)


class MonthGridTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        self.window = services.Window(anchor=JULY, mode="month")
        self.grid = services.month_grid(
            self.window, query(self.data.manager, window=self.window))

    def test_july_2026_needs_five_rows(self):
        self.assertEqual(len(self.grid["weeks"]), 5)
        self.assertTrue(all(len(week) == 7 for week in self.grid["weeks"]))

    def test_trailing_days_are_marked_as_other_month(self):
        first = self.grid["weeks"][0][0]
        self.assertEqual(first["date"], GRID_START)
        self.assertTrue(first["is_other_month"])

    def test_a_cell_caps_its_chips_but_never_its_count(self):
        """The cap is a display decision, not a data one — a busy day must not
        look like a quiet one."""
        for index in range(services.MONTH_CELL_LIMIT + 3):
            CalendarEvent.objects.create(title=f"Thing {index}",
                                         start_date=WEDNESDAY,
                                         created_by=self.data.manager)
        grid = services.month_grid(
            self.window, query(self.data.manager, window=self.window))
        cell = next(cell for week in grid["weeks"] for cell in week
                    if cell["date"] == WEDNESDAY)
        self.assertEqual(len(cell["events"]), services.MONTH_CELL_LIMIT)
        self.assertEqual(cell["hidden"],
                         cell["total"] - services.MONTH_CELL_LIMIT)
        self.assertGreater(cell["total"], services.MONTH_CELL_LIMIT)

    def test_weekends_are_marked(self):
        saturday = next(cell for week in self.grid["weeks"] for cell in week
                        if cell["date"].isoweekday() == 6)
        self.assertTrue(saturday["is_weekend"])


class WeekGridTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_all_day_entries_are_split_out_from_timed_ones(self):
        """An all-day marker stretched down a time axis is a lie about when it
        happens."""
        window = services.Window(anchor=MONDAY, mode="week")
        grid = services.week_grid(window, query(self.data.manager, window=window))
        monday = grid["columns"][0]
        self.assertTrue(any(e.title == "Weekly stand-up" for e in monday["timed"]))
        self.assertTrue(any(e.kind == "task" for e in monday["all_day"]))

    def test_seven_columns_starting_on_monday(self):
        window = services.Window(anchor=WEDNESDAY, mode="week")
        grid = services.week_grid(window, query(self.data.manager, window=window))
        self.assertEqual(len(grid["columns"]), 7)
        self.assertEqual(grid["columns"][0]["date"], MONDAY)


class DayAndAgendaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_a_day_shows_everything_that_covers_it(self):
        window = services.Window(anchor=TUESDAY, mode="day")
        detail = services.day_detail(
            window, query(self.data.manager, window=window))
        self.assertTrue(any(e.title == "Client review" for e in detail["timed"]))

    def test_a_day_in_the_middle_of_a_leave_span_still_shows_it(self):
        middle = MONDAY + timedelta(days=8)
        window = services.Window(anchor=middle, mode="day")
        detail = services.day_detail(
            window, query(self.data.manager, window=window))
        self.assertTrue(any(e.kind == "leave" for e in detail["events"]))

    def test_the_agenda_leaves_empty_days_out(self):
        """A grid is a map, so its blank cells mean something. An agenda is a
        list, and "nothing on Tuesday" rows are just scrolling."""
        window = services.Window(anchor=MONDAY, mode="agenda")
        listing = services.agenda(window, query(self.data.manager, window=window))
        self.assertTrue(all(day["events"] for day in listing["days"]))
        self.assertLess(len(listing["days"]), services.AGENDA_PAGE_DAYS)

    def test_the_agenda_is_in_date_order(self):
        window = services.Window(anchor=MONDAY, mode="agenda")
        listing = services.agenda(window, query(self.data.manager, window=window))
        dates = [day["date"] for day in listing["days"]]
        self.assertEqual(dates, sorted(dates))

    def test_the_agenda_total_counts_every_entry_not_every_day(self):
        window = services.Window(anchor=MONDAY, mode="agenda")
        listing = services.agenda(window, query(self.data.manager, window=window))
        self.assertEqual(listing["total"],
                         sum(len(day["events"]) for day in listing["days"]))


class StripTests(SimpleTestCase):
    def make(self, day, *, overdue=False, kind="task"):
        return Event(key=f"k{day}", kind=kind, title=f"T{day}",
                     start_date=day, is_overdue=overdue)

    def test_next_up_takes_the_soonest_and_skips_what_has_finished(self):
        events = [self.make(MONDAY - timedelta(days=3)),
                  self.make(WEDNESDAY), self.make(THURSDAY)]
        found = services.next_up(events, today=TUESDAY, limit=2)
        self.assertEqual([e.start_date for e in found], [WEDNESDAY, THURSDAY])

    def test_a_span_still_running_today_counts_as_coming_up(self):
        span = Event(key="s", kind="leave", title="Away",
                     start_date=MONDAY, end_date=FRIDAY)
        self.assertEqual(services.next_up([span], today=WEDNESDAY), [span])

    def test_overdue_lists_only_what_is_flagged(self):
        events = [self.make(MONDAY, overdue=True), self.make(TUESDAY)]
        self.assertEqual(len(services.overdue(events)), 1)

    def test_overdue_is_worst_first(self):
        events = [self.make(WEDNESDAY, overdue=True),
                  self.make(MONDAY, overdue=True)]
        self.assertEqual([e.start_date for e in services.overdue(events)],
                         [MONDAY, WEDNESDAY])


class VisibilityHelperTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_the_legend_only_offers_kinds_this_viewer_can_meet(self):
        """A colour key for a colour they will never see is noise, and it
        advertises the existence of data they aren't cleared for."""
        keys = {kind.key for kind in services.visible_kinds(self.data.sales)}
        self.assertNotIn("task", keys)
        self.assertNotIn("leave", keys)
        self.assertIn("meeting", keys)

    def test_a_manager_sees_the_whole_legend(self):
        keys = [kind.key for kind in services.visible_kinds(self.data.manager)]
        self.assertEqual(keys, KIND_ORDER)

    def test_movable_sources_is_one_answer_per_source(self):
        movable = services.movable_sources(self.data.manager)
        self.assertEqual(set(movable), {"task", "milestone", "project",
                                        "event", "document", "leave",
                                        "birthday", "client_activity"})
        self.assertFalse(movable["leave"])
        self.assertFalse(movable["birthday"])


class EventValueTests(SimpleTestCase):
    def test_a_single_day_event_ends_the_day_it_starts(self):
        event = Event(key="k", kind="task", title="T", start_date=MONDAY)
        self.assertEqual(event.end_date, MONDAY)
        self.assertEqual(event.days, 1)
        self.assertFalse(event.is_multi_day)

    def test_an_all_day_event_is_anchored_to_nine_am(self):
        """A reminder for "tomorrow's deadline" fired at 00:00 lands in the
        middle of the night."""
        event = Event(key="k", kind="task", title="T", start_date=MONDAY)
        self.assertEqual(event.start_datetime.time(), time(9, 0))

    def test_a_timed_event_keeps_its_own_time(self):
        event = Event(key="k", kind="meeting", title="T", start_date=MONDAY,
                      start_time=time(14, 30))
        self.assertEqual(event.start_datetime.time(), time(14, 30))

    def test_an_event_is_immutable(self):
        """An Event is a snapshot of a row somebody else owns; letting a view
        mutate one would create the illusion of an edit nothing writes down."""
        event = Event(key="k", kind="task", title="T", start_date=MONDAY)
        with self.assertRaises(Exception):
            event.title = "changed"

    def test_the_query_reports_which_filters_are_set(self):
        q = CalendarQuery(start=MONDAY, end=FRIDAY, employee_id=3)
        self.assertEqual(q.active_filters, {"employee"})
        self.assertTrue(q.is_filtered)

    def test_an_unfiltered_query_wants_every_kind(self):
        q = CalendarQuery(start=MONDAY, end=FRIDAY)
        self.assertTrue(all(q.wants(kind) for kind in KIND_ORDER))
        self.assertFalse(q.is_filtered)
