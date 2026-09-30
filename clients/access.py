"""Workspace visibility for clients.

Thin by design — a client has a workspace column, so there is exactly one rule
and `core.tenancy.scope` already knows it. It exists as a named helper anyway so
that the six places which list clients (the directory, the project form's
dropdown, analytics, the calendar filter bar, the document engine's pickers) all
say the same thing, and so widening the rule later is one edit rather than six.
"""
from core.tenancy import in_scope, scope


def visible_clients(qs, user):
    """Narrow a Client queryset to the viewer's workspaces."""
    return scope(qs, user)


def user_can_see_client(client, user):
    """Single-object form, without a query."""
    return in_scope(client.workspace_id, user)
