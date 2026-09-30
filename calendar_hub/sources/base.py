"""The source contract and the registry.

A source is the adapter between one existing app and the calendar. It answers
three questions and nothing else:

    may this viewer see my kind of thing?   -> can_view
    what lands in this window?              -> fetch
    may this viewer drag it, and where to?  -> can_move / move

Everything that makes the calendar an aggregator rather than a copy lives in
that shape. `fetch` reads the owning app's table live; no source is allowed to
write anything into `calendar_hub`. `move` writes back to the owning app,
through the owning app's own permission — dragging a task on the calendar is
task editing, and the calendar must not become a way around the task module's
rules. The resource planner set that precedent with `tasks.assign`; this follows
it.

Adding a ninth source is one file plus one import in `__init__.py`. Nothing in
`services.py`, the views or the templates knows how many sources there are.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied

from accounts.permissions import has_perm

# key -> instance, in registration order. Order matters only for the legend and
# for tie-breaking equal sort keys, so it follows the natural reading order of
# the module rather than anything computed.
REGISTRY: dict[str, "EventSource"] = {}


def register(source_class):
    """Class decorator. Instantiates once and files it under its key."""
    source = source_class()
    if source.key in REGISTRY:
        raise ImproperlyRegistered(f"Duplicate calendar source key: {source.key}")
    REGISTRY[source.key] = source
    return source_class


class ImproperlyRegistered(Exception):
    pass


def all_sources():
    return list(REGISTRY.values())


def get_source(key):
    return REGISTRY.get(key)


def sources_for(query):
    """The sources that can answer this query, in registration order.

    A source drops out for three separate reasons, all of them checked before
    any SQL runs:

    * the viewer lacks `view` on the module that owns the data;
    * none of the kinds it emits were asked for;
    * a filter is set that it has no way to honour.

    The third is the one worth spelling out. Filtering the calendar to one
    employee and still seeing every project milestone would be answering a
    different question from the one asked — a milestone has no assignee, so the
    honest response is to show none rather than to guess that "milestones on
    projects they've touched" is what was meant.
    """
    return [source for source in REGISTRY.values() if source.accepts(query)]


class EventSource:
    """Base class. Subclasses set the class attributes and implement `fetch`."""

    #: Short slug; the first half of every `Event.key` this source produces.
    key = ""
    #: Every `events.KINDS` key this source can emit.
    kinds: tuple = ()
    #: RBAC module key that governs seeing this data at all.
    module = ""
    #: The scope filters this source understands. Anything set outside this set
    #: silences the source — see `sources_for`.
    supports: frozenset = frozenset()
    #: RBAC action needed to change the underlying date. Empty means the source
    #: is read-only on the calendar.
    move_action = ""
    #: Human sentence used in the drag-and-drop refusal message.
    move_noun = "this"
    #: Whether the nightly job may send "due tomorrow" alerts for this source.
    #: Off by default — a source only opts in if its entries name the person the
    #: alert belongs to *and* its visibility does not depend on who is asking.
    #: See the deliberate limitation documented in `reminders.py`.
    sends_deadline_alerts = False

    # --- visibility ---

    def can_view(self, user):
        return has_perm(user, self.module, "view")

    def accepts(self, query):
        if not self.can_view(query.viewer):
            return False
        if not any(query.wants(kind) for kind in self.kinds):
            return False
        return query.active_filters <= self.supports

    # --- reading ---

    def fetch(self, query):
        """Yield `Event`s inside `query`'s window. One query, no N+1."""
        raise NotImplementedError

    # --- writing (drag-and-drop) ---

    @property
    def is_movable(self):
        return bool(self.move_action)

    def can_move(self, user):
        return self.is_movable and has_perm(user, self.module, self.move_action)

    def move(self, user, object_id, new_date, *, occurrence_date=None):
        """Reschedule the underlying record. Returns (object, description).

        Raises `PermissionDenied` if the viewer may not, and `LookupError` if
        the record is gone — both of which the view turns into JSON rather than
        letting a 500 reach a drag handler.

        The base implementation refuses. A read-only source reaches here when
        somebody posts a hand-built drag for something the UI never made
        draggable, and that has to be a refusal rather than a traceback.
        """
        self._require_move(user)
        raise NotImplementedError(
            f"{type(self).__name__} declares move_action but implements no move()")

    def _require_move(self, user):
        if not self.is_movable:
            raise PermissionDenied(
                f"{self.kinds[0].replace('_', ' ').capitalize()} entries can't "
                "be rescheduled from the calendar.")
        if not self.can_move(user):
            raise PermissionDenied(
                f"You don't have permission to reschedule {self.move_noun}.")


def search_filter(queryset, term, *fields):
    """Case-insensitive OR across `fields`, or the queryset untouched.

    Applied in SQL inside each source rather than in Python over the assembled
    event list: filtering after the fact would mean loading a month of
    everything to show four matches, and the point of searching is to not do
    that.
    """
    if not term:
        return queryset
    from django.db.models import Q

    condition = Q()
    for field in fields:
        condition |= Q(**{f"{field}__icontains": term})
    return queryset.filter(condition)
