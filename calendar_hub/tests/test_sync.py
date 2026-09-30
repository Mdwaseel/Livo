"""The external-provider seam.

No Google, Outlook or Apple integration exists yet, and these tests do not
pretend otherwise. What they check is the thing that would be expensive to get
wrong later: that the seam is real, that a provider plugs into it without any
other file changing, and that the identity rule which stops a sync from
duplicating everything actually holds.

The rest of "sync ready" is exercised by `test_ical.py`, which is the half that
works today.
"""
from datetime import date, time

from django.test import TestCase

from calendar_hub import sync
from calendar_hub.events import Event
from calendar_hub.models import CalendarEvent
from calendar_hub.sync.base import REGISTRY, CalendarProvider

from .factories import MONDAY, TUESDAY, Scenario


class FakeProvider(CalendarProvider):
    """Stands in for a real integration. Registered and torn down per test so
    the global registry never leaks between them."""

    key = "fake"
    label = "Fake Calendar"
    is_available = True

    def pull(self, account, start, end):
        return [Event(key="fake:1", kind="meeting", title="Remote sync",
                      start_date=MONDAY, start_time=time(11, 0),
                      external_id="remote-abc", source="fake")]


class ProviderRegistryTests(TestCase):
    def setUp(self):
        self.addCleanup(REGISTRY.pop, "fake", None)

    def test_a_provider_registers_and_is_retrievable(self):
        sync.register_provider(FakeProvider)
        self.assertIs(type(sync.get_provider("fake")), FakeProvider)
        self.assertIn("fake", {p.key for p in sync.all_providers()})

    def test_an_unknown_provider_is_none_rather_than_an_error(self):
        self.assertIsNone(sync.get_provider("nope"))

    def test_no_provider_ships_enabled(self):
        """`is_available` is what a settings screen reads. Anything in the
        registry that claimed to work without an implementation behind it would
        be offered to a user and then fail."""
        for provider in sync.all_providers():
            self.assertFalse(provider.is_available,
                             f"{provider.key} claims to be usable")

    def test_the_base_contract_refuses_to_pretend(self):
        """An unimplemented method raises rather than returning nothing — a
        silent empty pull looks exactly like "the remote calendar is empty"."""
        provider = CalendarProvider()
        with self.assertRaises(NotImplementedError):
            provider.pull(None, MONDAY, TUESDAY)
        with self.assertRaises(NotImplementedError):
            provider.push(None, None)
        with self.assertRaises(NotImplementedError):
            provider.delete(None, "x")


class UpsertTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def setUp(self):
        self.provider = FakeProvider()

    def test_a_pulled_event_becomes_a_local_row(self):
        pulled = self.provider.pull(None, MONDAY, TUESDAY)[0]
        stored = self.provider.upsert_local(pulled, created_by=self.data.manager)
        self.assertEqual(stored.title, "Remote sync")
        self.assertEqual(stored.external_provider, "fake")
        self.assertEqual(stored.external_id, "remote-abc")

    def test_pulling_twice_updates_one_row_rather_than_creating_two(self):
        """The single most common way a calendar sync goes wrong."""
        pulled = self.provider.pull(None, MONDAY, TUESDAY)[0]
        self.provider.upsert_local(pulled)
        self.provider.upsert_local(pulled)
        self.assertEqual(
            CalendarEvent.objects.filter(external_provider="fake").count(), 1)

    def test_a_changed_remote_event_overwrites_the_local_copy(self):
        pulled = self.provider.pull(None, MONDAY, TUESDAY)[0]
        self.provider.upsert_local(pulled)
        from dataclasses import replace

        moved = replace(pulled, title="Renamed", start_date=TUESDAY)
        stored = self.provider.upsert_local(moved)
        self.assertEqual(stored.title, "Renamed")
        self.assertEqual(stored.start_date, TUESDAY)
        self.assertEqual(
            CalendarEvent.objects.filter(external_provider="fake").count(), 1)

    def test_a_synced_event_shows_up_on_the_calendar_like_any_other(self):
        """The point of the seam: an imported appointment is an ordinary
        CalendarEvent, so every view, filter and export already handles it."""
        from calendar_hub import services
        from calendar_hub.events import CalendarQuery

        pulled = self.provider.pull(None, MONDAY, TUESDAY)[0]
        self.provider.upsert_local(pulled, created_by=self.data.manager)
        events = services.collect(CalendarQuery(
            start=MONDAY, end=TUESDAY, viewer=self.data.manager))
        self.assertIn("Remote sync", {event.title for event in events})

    def test_the_external_id_travels_back_out_onto_the_event(self):
        """So a push can recognise what it already sent."""
        from calendar_hub import services
        from calendar_hub.events import CalendarQuery

        pulled = self.provider.pull(None, MONDAY, TUESDAY)[0]
        self.provider.upsert_local(pulled, created_by=self.data.manager)
        events = services.collect(CalendarQuery(
            start=MONDAY, end=TUESDAY, viewer=self.data.manager))
        synced = next(e for e in events if e.title == "Remote sync")
        self.assertEqual(synced.external_id, "remote-abc")
