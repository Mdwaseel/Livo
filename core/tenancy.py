"""Workspace scoping — *whose data is it*.

The RBAC engine in `accounts.permissions` answers what a person may DO, and
`projects.access` answers which rows of a project they may see. Neither answers
the question a partner arrangement asks: two agencies share one install, and
each must be blind to the other's clients, projects, money and charts.

That is this module. Every root record carries a `workspace`, and every list,
aggregate and chart is narrowed to the workspaces the viewer is allowed into.

**The rules**

* A workspace user sees exactly their own workspace. There is no switcher and
  no way out — not by URL, not by primary key, not through a chart.
* A HOME user sees the home workspace. Ordinary home staff see nothing else.
* A HOME **super admin** may additionally switch into a partner workspace, or
  into ALL, from the topbar. The default on every login is home alone, so our
  own numbers stay our own numbers unless somebody deliberately looks wider.

**How it reaches the queries.** Threading a `request` through selectors,
services and chart builders in seven apps would have meant changing a hundred
signatures for one boolean. Instead the middleware parks the resolved scope on
a thread-local for the duration of the request, and `scope()` reads it.

Three things keep that honest:

* `current_scope(user)` prefers an explicitly-passed user over the thread-local
  whenever the two disagree, so a background job or a notification path that
  asks "what can Anna see" gets Anna's answer, not the answer for whoever
  happens to be browsing on this worker.
* No thread-local and no user means *system context* — unfiltered — matching
  the convention `projects.access` already set for the nightly jobs. Every
  user-facing caller passes a user or runs under the middleware.
* NULL workspace reads as HOME, never as "visible to everyone". A row that
  somehow escapes stamping fails closed towards us, not open towards a partner.
"""
import threading
from functools import wraps
from typing import NamedTuple

from django.core.exceptions import PermissionDenied
from django.db.models import Q

_state = threading.local()

# Session key holding the switcher's position for a home super admin.
SESSION_KEY = "active_workspace"
ALL = "all"          # the switcher's "every workspace" position


# ---------------------------------------------------------------------------
# workspace lookups
# ---------------------------------------------------------------------------

def home_workspace():
    """The install's own workspace, memoised for the life of a request.

    `scope()` needs it on every single queryset it narrows, and the middleware
    needs it again to resolve the viewer — a database round-trip apiece would be
    a real per-page cost for a value that changes about once in the lifetime of
    an install. The cache lives on the same thread-local the middleware clears,
    so it can never outlive the request that filled it, and outside a request it
    simply doesn't apply.
    """
    cached = getattr(_state, "home", None)
    if cached is not None:
        return cached
    from .models import Workspace
    workspace = Workspace.home()
    # Only memoise inside a managed request, which is the only context that
    # runs `deactivate()`. Caching outside one would let the value outlive the
    # database it came from — a shell session, or a test whose fixtures are
    # torn down under it — and a stale home workspace id is the one value in
    # this module that must never be wrong.
    if getattr(_state, "in_request", False):
        _state.home = workspace
        _state.home_id = workspace.pk
    return workspace


def home_workspace_id():
    cached = getattr(_state, "home_id", None)
    if cached is None:
        cached = home_workspace().pk
    return cached


def user_workspace(user):
    """The workspace a person belongs to. Unset means home.

    Deliberately routed through `workspace_id` rather than `user.workspace`:
    touching the descriptor fetches the row, and for the common case — one of
    our own staff — that is a query for an object we already hold cached.
    """
    if not getattr(user, "is_authenticated", False):
        return None
    workspace_id = getattr(user, "workspace_id", None)
    if workspace_id is None or workspace_id == home_workspace_id():
        return home_workspace()
    return user.workspace


def is_home_user(user):
    """True for our own staff — the people whose data a partner must not see.

    Also the gate on payroll and on the workspace admin screens: a partner is a
    super admin *of their own world*, not of ours.

    Compares ids rather than loading the workspace, so the check costs nothing
    on the overwhelmingly common path.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    workspace_id = getattr(user, "workspace_id", None)
    return workspace_id is None or workspace_id == home_workspace_id()


def can_switch_workspace(user):
    """Only a home super admin gets the topbar switcher."""
    from accounts.permissions import is_superadmin
    return is_home_user(user) and is_superadmin(user)


def switchable_workspaces(user):
    from .models import Workspace
    if not can_switch_workspace(user):
        return Workspace.objects.none()
    return Workspace.objects.filter(is_active=True)


def is_home_admin(user):
    """Super admin OF THE HOME WORKSPACE — the only people who administer the
    partitions themselves, and the only ones who may edit configuration that is
    shared across all of them (the role matrix, agency branding, the org chart).

    Distinct from `is_superadmin`, and the distinction is the feature: a partner
    is a super admin of their own world. `has_perm` returns True for them on
    every module, so anything shared needs this gate rather than an RBAC one.
    """
    from accounts.permissions import is_superadmin
    return is_superadmin(user) and is_home_user(user)


def home_user_required(view):
    """403 for anyone outside the home workspace, whatever their role.

    The gate for editing configuration that is SHARED by every workspace but is
    not super-admin work — document templates, agency branding. The RBAC action
    still decides who among our own staff may edit it; this only adds "and they
    must be ours", which `has_perm` can't express because a partner super admin
    passes every action check there is.
    """
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not is_home_user(request.user):
            raise PermissionDenied(
                "This setting is shared by every workspace and is managed by "
                "the agency that owns this install.")
        return view(request, *args, **kwargs)
    return wrapper


def home_admin_required(view):
    """403 for anyone who isn't a home super admin.

    A 403 rather than the app's usual redirect-with-a-message: these URLs are
    not linked for anyone who can't use them, so reaching one is a hand-typed
    address, and "you may not administer other people's workspaces" is not a
    message a partner needs help understanding.
    """
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not is_home_admin(request.user):
            raise PermissionDenied("Restricted to your agency's administrators.")
        return view(request, *args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# scope resolution
# ---------------------------------------------------------------------------

class Scope(NamedTuple):
    """A resolved answer to "whose data may this viewer see".

    `includes_home` is carried rather than recomputed because `scope()` needs
    it on every queryset it narrows — NULL means home — and working it out
    there would put a Workspace lookup behind every list on every page.
    Deciding it once, where the home workspace is being read anyway, is what
    keeps partitioning a per-request cost rather than a per-query one.
    """

    ids: object            # frozenset of workspace ids, or None = unrestricted
    workspace: object      # the single workspace in view, None under ALL
    includes_home: bool


# Unrestricted, for callers with no user and no request: the nightly reminder
# job, `generate_recurring_tasks`, a management command, a shell.
SYSTEM = Scope(ids=None, workspace=None, includes_home=True)
# Signed out. Nothing is visible — the login page queries none of this anyway.
ANONYMOUS = Scope(ids=frozenset(), workspace=None, includes_home=False)


def resolve_scope(user, session=None):
    """The Scope for this viewer, reading the switcher out of `session`.

    Unrestricted (`ids=None`) is reserved for the ALL position of the home
    super admin's switcher and for system context.
    """
    from .models import Workspace

    if not getattr(user, "is_authenticated", False):
        return ANONYMOUS

    own = user_workspace(user)
    home_id = home_workspace_id()

    def pinned(workspace):
        return Scope(frozenset({workspace.pk}), workspace,
                     workspace.pk == home_id)

    if not can_switch_workspace(user):
        # No switcher means pinned to your own workspace, full stop — and that
        # is everybody except a home super admin, partners very much included.
        return pinned(own)

    choice = (session or {}).get(SESSION_KEY) if session is not None else None
    if choice == ALL:
        return Scope(None, None, True)
    if choice:
        target = Workspace.objects.filter(pk=choice, is_active=True).first()
        if target is not None:
            return pinned(target)
    return pinned(own)


# ---------------------------------------------------------------------------
# per-request state
# ---------------------------------------------------------------------------

def activate(user, resolved):
    _state.user_pk = getattr(user, "pk", None)
    _state.resolved = resolved


def deactivate():
    for attr in ("in_request", "user_pk", "resolved", "home", "home_id"):
        if hasattr(_state, attr):
            delattr(_state, attr)


def _thread_scope_for(user):
    """The parked Scope, but only when it belongs to the user being asked about.

    Returns a Scope, or None when the caller must derive one from the user.
    """
    if not hasattr(_state, "resolved"):
        return None
    if user is not None and getattr(user, "pk", None) != getattr(_state, "user_pk", None):
        return None
    return _state.resolved


def current_scope(user=None):
    """The Scope in force: the parked one, one derived from `user`, or SYSTEM.

    A scope derived from a user is memoised on the user object, the same way
    `accounts.permissions` memoises its permission cache. That is what keeps a
    caller which asks seven times in a row — `calendar_hub.services.collect`
    runs one source per calendar layer — down to a single resolution, whether
    or not it happens to be running inside a request.
    """
    parked = _thread_scope_for(user)
    if parked is not None:
        return parked
    if user is None:
        # System context: a management command, a nightly job, a shell.
        # Unfiltered, the same way `projects.access` treats `user=None`.
        return SYSTEM
    cached = getattr(user, "_workspace_scope", None)
    if cached is None:
        cached = resolve_scope(user)
        try:
            user._workspace_scope = cached
        except AttributeError:      # an object that will not take attributes
            pass
    return cached


def active_workspace_ids(user=None):
    """The workspace ids in reach. `None` means unrestricted."""
    return current_scope(user).ids


def active_workspace(user=None):
    """The single workspace in view, or None under ALL / system context."""
    return current_scope(user).workspace


def workspace_for_new(user):
    """Which workspace a record this person is creating belongs to.

    Under ALL there is no single answer, so it falls back to the creator's own
    workspace — a home admin browsing everything who creates a client is
    creating OUR client, not an unattributed one.
    """
    if not getattr(user, "is_authenticated", False):
        return None
    return active_workspace(user) or user_workspace(user)


# ---------------------------------------------------------------------------
# the queryset filter
# ---------------------------------------------------------------------------

def scope(qs, user=None, path="workspace"):
    """Narrow `qs` to the viewer's workspaces.

    `path` is the ORM path from this model to the Workspace — "workspace" for a
    stamped model, "project__workspace" for anything hanging off a project.
    NULL is folded into home, so legacy rows written before this shipped stay
    where they belong instead of vanishing or leaking.
    """
    resolved = current_scope(user)
    if resolved.ids is None:
        return qs
    if not resolved.ids:
        return qs.none()
    condition = Q(**{f"{path}_id__in": resolved.ids})
    if resolved.includes_home:
        condition |= Q(**{f"{path}__isnull": True})
    return qs.filter(condition)


def restrict_to(qs, workspace_id, path="workspace"):
    """Narrow `qs` to one named workspace, regardless of who is asking.

    Distinct from `scope`, which answers "what may the viewer see". This answers
    "who belongs to this record's world" — the question the notification paths
    ask, where the actor is irrelevant and the record decides the audience.
    """
    if workspace_id is None:
        workspace_id = home_workspace_id()
    condition = Q(**{f"{path}_id": workspace_id})
    if workspace_id == home_workspace_id():
        condition |= Q(**{f"{path}__isnull": True})
    return qs.filter(condition)


def in_scope(workspace_id, user=None):
    """Single-object form of `scope`, without a query."""
    resolved = current_scope(user)
    if resolved.ids is None:
        return True
    if workspace_id is None:
        return resolved.includes_home
    return workspace_id in resolved.ids


# ---------------------------------------------------------------------------
# middleware
# ---------------------------------------------------------------------------

class WorkspaceMiddleware:
    """Park the request's workspace scope where `scope()` can find it.

    Must sit after AuthenticationMiddleware and SessionMiddleware. The
    try/finally is not optional: a thread that keeps a stale scope after an
    exception would hand the next request on that worker somebody else's
    partition.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        # Armed before resolving, not after: resolving itself reads the home
        # workspace, and without the flag that read would happen twice — once
        # to find the viewer's own workspace and once to decide whether their
        # scope covers home.
        _state.in_request = True
        resolved = resolve_scope(user, getattr(request, "session", None))
        activate(user, resolved)
        request.workspace = resolved.workspace
        request.workspace_ids = resolved.ids
        try:
            return self.get_response(request)
        finally:
            deactivate()
