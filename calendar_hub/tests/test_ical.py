"""iCalendar serialisation.

Written by hand, so the parts that actually get files rejected are the parts
worth testing: line folding at 75 octets, escaping of the characters that are
special inside a property value, a stable UID, and the exclusive DTEND that
all-day events need.
"""
from datetime import date, datetime, time, timedelta

from django.test import SimpleTestCase

from calendar_hub.events import Event
from calendar_hub.services import Window
from calendar_hub.sync import ical

MONDAY = date(2026, 7, 6)


def one(**kwargs):
    kwargs.setdefault("key", "task:7")
    kwargs.setdefault("kind", "task")
    kwargs.setdefault("title", "Build header")
    kwargs.setdefault("start_date", MONDAY)
    return Event(**kwargs)


def lines(body):
    return body.split("\r\n")


class StructureTests(SimpleTestCase):
    def test_a_calendar_is_well_formed(self):
        body = ical.to_ics([one()])
        self.assertTrue(body.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("VERSION:2.0", body)
        self.assertIn("END:VCALENDAR", body)
        self.assertEqual(body.count("BEGIN:VEVENT"), 1)
        self.assertEqual(body.count("END:VEVENT"), 1)

    def test_crlf_line_endings(self):
        """The spec says so, and Outlook is the client that notices."""
        body = ical.to_ics([one()])
        self.assertNotIn("\n", body.replace("\r\n", ""))

    def test_an_empty_calendar_is_still_valid(self):
        body = ical.to_ics([])
        self.assertIn("BEGIN:VCALENDAR", body)
        self.assertNotIn("BEGIN:VEVENT", body)

    def test_every_event_becomes_one_vevent(self):
        body = ical.to_ics([one(key="task:1"), one(key="task:2"),
                            one(key="task:3")])
        self.assertEqual(body.count("BEGIN:VEVENT"), 3)


class DateTests(SimpleTestCase):
    def test_an_all_day_event_ends_the_day_after_it_finishes(self):
        """A DTEND equal to DTSTART is a zero-length event, which some clients
        silently drop."""
        body = ical.to_ics([one()])
        self.assertIn("DTSTART;VALUE=DATE:20260706", body)
        self.assertIn("DTEND;VALUE=DATE:20260707", body)

    def test_a_multi_day_all_day_event_spans_correctly(self):
        body = ical.to_ics([one(end_date=MONDAY + timedelta(days=2))])
        self.assertIn("DTSTART;VALUE=DATE:20260706", body)
        self.assertIn("DTEND;VALUE=DATE:20260709", body)

    def test_a_timed_event_carries_its_clock_time(self):
        body = ical.to_ics([one(start_time=time(14, 0), end_time=time(15, 30))])
        self.assertIn("DTSTART:20260706T140000", body)
        self.assertIn("DTEND:20260706T153000", body)

    def test_a_timed_event_with_no_end_falls_back_to_its_start(self):
        body = ical.to_ics([one(start_time=time(14, 0))])
        self.assertIn("DTEND:20260706T140000", body)

    def test_a_stamp_is_emitted_when_given(self):
        body = ical.to_ics([one()], stamp=datetime(2026, 7, 26, 12, 30, 5))
        self.assertIn("DTSTAMP:20260726T123005Z", body)


class EscapingTests(SimpleTestCase):
    def test_commas_and_semicolons_are_escaped(self):
        """A client called "Acme, Inc." breaks an unescaped feed."""
        body = ical.to_ics([one(title="Acme, Inc.; final")])
        self.assertIn("SUMMARY:Acme\\, Inc.\\; final", body)

    def test_backslashes_are_escaped_first(self):
        """Escaping them last would double-escape everything introduced by the
        other rules."""
        body = ical.to_ics([one(title=r"a\b,c")])
        self.assertIn(r"SUMMARY:a\\b\,c", body)

    def test_newlines_become_the_literal_escape(self):
        body = ical.to_ics([one(title="line one\nline two")])
        self.assertIn(r"SUMMARY:line one\nline two", body)
        # And the folded output must not contain a bare newline in the value.
        self.assertNotIn("line one\r\nline two", body)


class FoldingTests(SimpleTestCase):
    def test_a_long_line_is_folded_with_a_leading_space(self):
        body = ical.to_ics([one(title="Q" * 200)])
        for line in lines(body):
            self.assertLessEqual(len(line.encode("utf-8")), ical.LINE_LIMIT)
        self.assertIn("\r\n Q", body)

    def test_folding_counts_octets_not_characters(self):
        """The limit in the spec is octets, and the em-dashes and curly quotes
        this codebase uses freely are longer than they look."""
        body = ical.to_ics([one(title="—" * 60)])
        for line in lines(body):
            self.assertLessEqual(len(line.encode("utf-8")), ical.LINE_LIMIT)

    def test_a_short_line_is_left_alone(self):
        body = ical.to_ics([one(title="Short")])
        self.assertIn("SUMMARY:Short\r\n", body)

    def test_a_folded_value_unfolds_back_to_the_original(self):
        """Unfolding is "remove CRLF followed by a space". If that doesn't
        recover the value, the fold was wrong."""
        title = "Z" * 300
        body = ical.to_ics([one(title=title)])
        unfolded = body.replace("\r\n ", "")
        self.assertIn(f"SUMMARY:{title}", unfolded)


class IdentityTests(SimpleTestCase):
    def test_the_uid_is_derived_from_the_event_key(self):
        """Re-importing a feed has to update the same appointment rather than
        stacking a second copy of it."""
        body = ical.to_ics([one(key="task:7")])
        self.assertIn("UID:task-7@livo", body)

    def test_two_occurrences_of_a_series_get_different_uids(self):
        first = ical.to_ics([one(key="event:3@2026-07-06")])
        second = ical.to_ics([one(key="event:3@2026-07-13")])
        self.assertNotEqual(
            [line for line in lines(first) if line.startswith("UID")],
            [line for line in lines(second) if line.startswith("UID")])

    def test_the_kind_survives_as_a_category(self):
        """So a subscriber can colour by it in their own client — the colour
        coding survives the export."""
        self.assertIn("CATEGORIES:Task due", ical.to_ics([one()]))

    def test_the_url_is_carried_through(self):
        self.assertIn("URL:/projects/tasks/7/",
                      ical.to_ics([one(url="/projects/tasks/7/")]))

    def test_a_filename_says_what_is_inside_it(self):
        window = Window(anchor=date(2026, 7, 1), mode="month")
        self.assertEqual(ical.filename_for(window),
                         "livo-calendar-2026-07-01-to-2026-07-31.ics")


class ProviderSeamTests(SimpleTestCase):
    def test_the_provider_registry_starts_empty_and_advertises_nothing(self):
        """No integration ships. What ships is the shape one will have, plus an
        export all three vendors can already read."""
        from calendar_hub import sync

        self.assertEqual(sync.all_providers(), [])
        self.assertIsNone(sync.get_provider("google"))

    def test_the_base_provider_refuses_to_pretend(self):
        from calendar_hub.sync import CalendarProvider

        provider = CalendarProvider()
        self.assertFalse(provider.is_available)
        with self.assertRaises(NotImplementedError):
            provider.pull(None, MONDAY, MONDAY)
        with self.assertRaises(NotImplementedError):
            provider.push(None, one())
        with self.assertRaises(NotImplementedError):
            provider.delete(None, "abc")
