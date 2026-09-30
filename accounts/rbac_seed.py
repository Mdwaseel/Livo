"""
RBAC seed data + logic, shared by the seed_rbac management command and the
0003_seed_rbac data migration (which runs it with historical models so fresh
databases — including test databases — always have the catalog).

Idempotent. Matrices are only written for roles that have no permission rows
yet, so admin edits made in the UI are never overwritten.
"""

MODULES = [
    # (key, label)
    ("dashboard", "Dashboard"), ("clients", "Clients"), ("contacts", "Contacts"),
    ("projects", "Projects"), ("milestones", "Milestones"), ("tasks", "Tasks"),
    ("worklogs", "Work Logs"), ("assets", "Assets"), ("leads", "CRM / Leads"),
    ("deals", "Deals"), ("invoices", "Invoices"), ("quotations", "Quotations"),
    ("documents", "Documents"), ("templates", "Templates"),
    ("employees", "Employees"), ("attendance", "Attendance"), ("leaves", "Leaves"),
    ("payroll", "Payroll"), ("finance", "Finance"), ("reports", "Reports"),
    ("analytics", "Analytics"), ("resource_planning", "Resource Planning"),
    ("notifications", "Notifications"),
    ("knowledgebase", "Knowledge Base"), ("calendar", "Calendar"),
    ("business", "Business Overview"),
    ("agency_settings", "Agency Settings"), ("integrations", "Integrations"),
    ("audit_logs", "Audit Logs"), ("api_keys", "API Keys"),
]

# Keep in sync with accounts.models.ACTIONS (copied here so historical-model
# migrations never import live model code).
ACTIONS = ("view", "create", "edit", "delete", "approve",
           "export", "import", "assign", "archive", "restore", "view_all")

ALL = set(ACTIONS)
V = {"view"}
RW = {"view", "create", "edit"}
RWD = {"view", "create", "edit", "delete"}

# (name, is_system, priority, description)
ROLES = [
    ("Super Admin", True, 0,
     "Full access to everything, including settings and API keys. Bypasses checks."),
    ("Manager", True, 10,
     "Everything except Agency Settings and API keys."),
    ("Project Manager", False, 20,
     "Delivery lead: runs projects, tasks and documents (edit + approval). "
     "Adding/deleting clients, projects and documents is Manager/Super Admin only."),
    ("Developer", False, 40,
     "Delivery work: sees projects and updates own tasks/work logs. No finance/HR/CRM."),
    ("Designer", False, 41,
     "Same delivery scope as Developer."),
    ("Sales", False, 30,
     "CRM: leads, deals, quotations; view invoices. No projects/tasks."),
    ("Accounts", False, 35,
     "Finance: invoices, payments, reports, clients. No projects/HR."),
    ("HR", False, 36,
     "People: maintains employee records, attendance, leaves and payroll. "
     "Onboarding/removing employees is Manager/Super Admin only."),
    ("Client", False, 90,
     "External portal user: read-only view of their projects and documents."),
]

_BASE = {"dashboard": V, "notifications": V, "calendar": RW, "knowledgebase": RW}
# Matches the legacy "delivery" write scope (DEV/Designer could manage tasks,
# work logs, assets and milestones and edit project overview fields), so
# existing teams lose nothing in the migration. Tighten per-role in the UI.
_DELIVERY = {
    **_BASE,
    # Read-only on clients: delivery needs to know WHO a project is for. The
    # `view` action is enforced now (clients.views.client_list/detail), so an
    # omission here is a locked door, not a hidden menu item.
    "clients": V,
    "contacts": V,
    "projects": V | {"edit"},
    "milestones": RWD,
    "tasks": RWD,
    "worklogs": RWD,
    "assets": {"view", "create", "delete"},
    # `export` is what documents.views.document_export_pdf checks now — it used
    # to check nothing at all. Delivery pulls PDFs of SRSs and contracts as a
    # matter of course, so granting it here keeps that working rather than
    # quietly taking it away along with the fix.
    "documents": V | {"export"},
}
MATRICES = {
    "Super Admin": {key: ALL for key, _ in MODULES},
    # `business` is excluded deliberately, alongside settings and API keys. It
    # is the confidential money view — revenue, outstanding, collection rate —
    # and the whole point of that screen is that it is not on by default for
    # anyone. Tick it for a role in /settings/roles/ to hand it to somebody.
    "Manager": {key: ALL for key, _ in MODULES
                if key not in ("agency_settings", "api_keys", "business")},
    "Project Manager": {
        **_BASE,
        # Adding/deleting clients, projects and documents is reserved for
        # Manager / Super Admin; PMs still run and edit everything else.
        "clients": V | {"edit", "assign", "export"},
        "contacts": RWD,
        # `view_all` is excluded deliberately: a PM sees the projects they are a
        # member of, not every project in the agency. Tick it in /settings/roles/
        # for a PM who really does run everything.
        "projects": ALL - {"import", "create", "delete", "view_all"},
        "milestones": RWD | {"assign", "approve"},
        "tasks": RWD | {"assign"},
        "worklogs": RWD,
        "assets": RWD | {"archive", "restore"},
        "documents": V | {"edit", "approve", "export", "archive", "restore"},
        "templates": V,
        "quotations": RW | {"export"},
        "invoices": V,
        "reports": RW | {"export"},
        # `view` only, never `export`: a PM reads the delivery picture for the
        # work they run, but a spreadsheet of every employee's hours is not a
        # thing that should leave the building on a PM's say-so. Money stays
        # gated a second time by `finance.view`, so the revenue columns are
        # simply absent here — the same split the project page already makes.
        "analytics": V,
        # `resource_planning` is still Super Admin / Manager only, and for a
        # reason analytics does not share: the planner board lists per-assignee
        # tasks DIRECTLY rather than aggregating them, so opening it would make
        # per-assignee task visibility cosmetic. Grant it per role in
        # /settings/roles/ if a PM genuinely runs the whole floor.
        "employees": V,
    },
    "Developer": dict(_DELIVERY),
    "Designer": dict(_DELIVERY),
    "Sales": {
        **_BASE,
        "leads": RWD | {"assign", "export", "import"},
        "deals": RWD | {"assign", "export"},
        "quotations": RWD | {"export"},
        "invoices": V,
        # Read-only on projects: you can't quote for work you can't see. Still
        # no create/edit/delete here and no tasks module at all, so Sales gets
        # the delivery picture without any say in it.
        "projects": V,
        # No create/delete on clients or documents — that's Manager / Super
        # Admin only. Sales still edits the records it touches.
        "clients": V | {"edit"},
        "contacts": RWD,
        "documents": V | {"edit", "export"},
        "reports": V,
    },
    "Accounts": {
        **_BASE,
        # Read-only on projects: payments are recorded against a project and
        # every finance view redirects back to it, so without `view` Accounts
        # would be bounced off the page it just wrote to.
        "projects": V,
        "finance": RWD | {"export", "approve"},
        "invoices": RWD | {"export", "approve"},
        "quotations": V | {"export"},
        "clients": V | {"edit"},
        "contacts": V,
        "documents": V | {"edit", "export"},
        # Monthly-report generation is limited to Super Admin / Manager / PM.
        "reports": V | {"export"},
        # The revenue trend behind the payments Accounts records. `view` only,
        # matching the PM grant above — see migration 0012.
        "analytics": V,
    },
    "HR": {
        **_BASE,
        # Onboarding (add) and removing employees is Manager / Super Admin
        # only; HR still maintains records and runs attendance/leave/payroll.
        "employees": V | {"edit", "export"},
        "attendance": RWD | {"approve", "export"},
        "leaves": RWD | {"approve"},
        "payroll": RWD | {"approve", "export"},
        # `resource_planning` removed with the same reasoning as for the PM
        # above: the planner board exposes every person's task list and hours.
        "reports": V,
    },
    # NOT SAFE TO ASSIGN YET. This is the shape the M8 portal will use, but no
    # view scopes its queryset to the viewer's own client — a Client-role user
    # today would read every client's projects, documents and invoices. Leave
    # it unassigned until per-client scoping ships.
    "Client": {
        "dashboard": V, "projects": V, "documents": V | {"export"},
        "invoices": V | {"export"}, "quotations": V, "notifications": V,
    },
}

# Deprecated User.role CharField -> new Role name.
LEGACY_USER_ROLE_MAP = {
    "OWNER": "Super Admin",
    "PM": "Project Manager",
    "SALES": "Sales",
    "DEV": "Developer",
    "ACCT": "Accounts",
}


def seed_all(Module, Role, RolePermission, User, log=lambda msg: None):
    """Seed modules, roles, matrices; backfill users. Model classes are passed
    in so this works with both live and historical (migration) models."""
    modules = {}
    for order, (key, label) in enumerate(MODULES):
        mod, created = Module.objects.update_or_create(
            key=key, defaults={"label": label, "order": order})
        modules[key] = mod
        if created:
            log(f"  module + {key}")

    for name, is_system, priority, description in ROLES:
        role, created = Role.objects.get_or_create(
            name=name, defaults={"is_system": is_system, "priority": priority,
                                 "description": description})
        if created:
            log(f"  role   + {name}")
        if RolePermission.objects.filter(role=role).exists():
            continue  # never clobber a matrix an admin has edited
        matrix = MATRICES.get(name, {})
        # Only set flags the passed-in model actually has. This function is
        # shared by `manage.py seed_rbac` (live model) and the 0003 data
        # migration (historical model), and ACTIONS grows over time — handing a
        # historical RolePermission a column added three migrations later is a
        # TypeError that breaks every fresh database, test ones included.
        available = {f.name for f in RolePermission._meta.get_fields()}
        rows = [
            RolePermission(
                role=role, module=modules[key],
                **{f"can_{a}": (a in actions)
                   for a in ACTIONS if f"can_{a}" in available})
            for key, actions in matrix.items() if key in modules
        ]
        RolePermission.objects.bulk_create(rows)
        log(f"  matrix   {name}: {len(rows)} modules")

    mapped = 0
    for user in User.objects.filter(primary_role__isnull=True):
        role_name = LEGACY_USER_ROLE_MAP.get(user.role)
        if role_name:
            user.primary_role = Role.objects.get(name=role_name)
            user.save(update_fields=["primary_role"])
            mapped += 1
            log(f"  user     {user.username}: {user.role} -> {role_name}")
    return mapped
