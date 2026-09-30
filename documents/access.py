"""Finance-visibility gating for the document engine.

Invoices, quotations and proposals are the agency's *financial* documents.
Two independent things gate them:

  * `finance.view`  — the blanket money switch. Also gates the ₹ figures
    (received / outstanding / due) rendered around the app, so a super admin
    can hand someone the whole finance picture with one toggle.
  * the document's OWN module (`invoices`, `quotations`) — the fine-grained
    switch, so Sales can run quotations without also seeing every payment
    figure in the agency, and a portal Client can read their own invoices.

Either one grants sight of that document type. Money figures stay on
`can_view_finance` alone — a Sales rep with `quotations.view` sees quotations,
not the agency's cash position.
"""
from accounts.permissions import has_perm
from core.tenancy import scope

# DocumentType slug -> the RBAC module that also grants it. Constants rather
# than a model flag because the set is small, fixed by seed_doctypes, and used
# inside querysets that must stay index-friendly.
#
# Proposals ride with quotations: both are pre-deal sales paper, and the module
# catalog has no separate "proposals" entry.
FINANCIAL_DOC_MODULES = {
    "invoice": "invoices",
    "quotation": "quotations",
    "proposal": "quotations",
}
FINANCIAL_DOC_SLUGS = tuple(FINANCIAL_DOC_MODULES)


def can_view_finance(user):
    """The money-figures gate: ₹ totals, outstanding, payment history.
    Backed by the Finance module's `view` action."""
    return has_perm(user, "finance", "view")


def can_view_doc_slug(user, slug):
    """Whether this user may see documents of `slug`.

    Non-financial types are open to anyone who can reach the documents module;
    financial ones need either blanket finance visibility or `view` on the
    type's own module.
    """
    module = FINANCIAL_DOC_MODULES.get(slug)
    if module is None:
        return True
    return can_view_finance(user) or has_perm(user, module, "view")


def is_financial_type(doc_type):
    """True for the DocumentType of a financial document."""
    return bool(doc_type) and doc_type.slug in FINANCIAL_DOC_MODULES


def can_view_doc_type(user, doc_type):
    """`can_view_doc_slug` for a DocumentType instance."""
    return bool(doc_type) and can_view_doc_slug(user, doc_type.slug)


def hidden_doc_slugs(user):
    """The financial slugs this user may NOT see — the exclusion list both
    queryset filters below are built from."""
    return tuple(s for s in FINANCIAL_DOC_MODULES if not can_view_doc_slug(user, s))


def visible_documents(qs, user):
    """Drop financial documents the user has no route to, from a Document
    queryset — and anything outside the viewer's workspace.

    A document has no workspace of its own: it hangs off a project, and a
    project's partition is the document's. Scoping through that FK rather than
    duplicating the column keeps one source of truth for "whose is this".
    """
    qs = scope(qs, user, path="project__workspace")
    hidden = hidden_doc_slugs(user)
    return qs.exclude(document_type__slug__in=hidden) if hidden else qs


def visible_doc_types(qs, user):
    """Same, for a DocumentType queryset (create menus, filter dropdowns)."""
    hidden = hidden_doc_slugs(user)
    return qs.exclude(slug__in=hidden) if hidden else qs
