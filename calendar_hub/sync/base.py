"""The external-provider seam.

Nothing here talks to Google, Outlook or Apple. What it does is fix the shape
such an integration will have, so that adding one later is a new file in this
package plus a credential store — and *no* change to any of the seven source
adapters, to `services.py`, to the views, or to a single business rule.

The seam is `events.Event`. A provider is defined as a thing that turns remote
appointments into `Event`s and local `Event`s into remote appointments:

    pull(account, start, end) -> Iterable[Event]
    push(account, event)      -> remote id

That works because the calendar already treats `Event` as the only currency
between the apps it reads and the screens it draws. A Google provider becomes an
eighth source, registered exactly like `tasks` or `leave`, and the month grid
gains a Google column without knowing that is what happened.

Three details are settled here rather than left to the first implementation,
because getting them wrong later means a migration:

* **Identity.** `CalendarEvent.external_provider` + `external_id` carry the
  remote key, under a unique constraint, so a pull that runs twice updates one
  row instead of creating two. Local events leave both blank.
* **Recurrence.** The rule stored on `CalendarEvent` is a strict subset of
  RFC 5545 RRULE — FREQ, INTERVAL, BYDAY, UNTIL, COUNT — and `ical.py` emits it
  verbatim. What cannot be expressed is *absent* rather than approximated, so a
  round-trip never quietly changes when a meeting happens.
* **Exceptions.** `EventOccurrence` is RECURRENCE-ID and EXDATE, the same model
  all three vendors use. Moving one stand-up survives a sync in both directions.

The one thing intentionally *not* built is authentication: OAuth token storage,
refresh and revocation are a security design with real consequences, and
guessing at them would be worse than leaving the seam clean. `ICalendarFeed`
below is the working half — a standards-compliant export that all three
calendars can already read.
"""
from __future__ import annotations

REGISTRY: dict[str, "CalendarProvider"] = {}


def register_provider(provider_class):
    provider = provider_class()
    REGISTRY[provider.key] = provider
    return provider_class


def all_providers():
    return list(REGISTRY.values())


def get_provider(key):
    return REGISTRY.get(key)


class CalendarProvider:
    """What an external calendar integration has to implement.

    Deliberately narrow. A provider does not get to decide what a meeting means,
    who may see it, or how it is displayed — those answers already exist and are
    not the remote service's business. It converts, and that is all.
    """

    #: Short slug stored in `CalendarEvent.external_provider`.
    key = ""
    #: Shown in the UI when the integration is offered.
    label = ""
    #: False until an implementation exists; the settings screen reads this
    #: rather than assuming everything in the registry is usable.
    is_available = False

    def pull(self, account, start, end):
        """Remote appointments in the window, as `events.Event` objects."""
        raise NotImplementedError

    def push(self, account, event):
        """Create or update `event` remotely. Returns the remote id."""
        raise NotImplementedError

    def delete(self, account, external_id):
        raise NotImplementedError

    # --- shared plumbing an implementation gets for free ---

    def upsert_local(self, event, *, created_by=None):
        """Store a pulled `Event` as a local `CalendarEvent`, once.

        Keyed on (provider, external_id) so re-running a pull updates rather
        than duplicates — the single most common way a calendar sync goes
        wrong.
        """
        from ..models import CalendarEvent

        obj, _ = CalendarEvent.objects.update_or_create(
            external_provider=self.key,
            external_id=event.external_id,
            defaults={
                "kind": CalendarEvent.Kind.MEETING,
                "title": event.title,
                "description": event.detail,
                "start_date": event.start_date,
                "end_date": event.end_date,
                "start_time": event.start_time,
                "end_time": event.end_time,
                "created_by": created_by,
            },
        )
        return obj
