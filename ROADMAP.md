# Livo OS — Build Roadmap

**Hierarchy:** `Client → Project → Documents`.
Every client auto-gets a default project, so simple one-off clients feel like
"Client → Documents" while multi-engagement clients still get clean structure.

**Core principle:** one config-driven document engine, not one app per document type.
A document type = a `DocumentType` row (form fields + AI prompt + template). Adding a
new type is data entry, not code.

---

## ✅ M0 — Scaffold  (DONE)
Django 5 project, 6 apps, custom User+roles, SQLite/PostgreSQL via env, base console UI, auth.

## ✅ M1 — Core data models  (DONE)
Client, Contact, Project, Milestone, DocumentType, Document, DocumentVersion,
Template, GeneratedFile, ActivityLog, AgencySettings. Full admin. Default-project signal.

## ✅ M2 — Client-centric foundation  (DONE)
Dashboard, client list/create/detail, project create/detail, document types seeded.

## ✅ M3 — Document engine end-to-end  (DONE — Proposal proven)
Pick type → dynamic form from schema → AI draft → Quill editor → save version →
version history. Works for all 10 seeded types through the same pipeline.

## ✅ M4 — Harden the generators  (DONE)
- AI wiring live: Groq Cloud (`GROQ_API_KEYS` comma-separated for key rotation
  in `.env`); stdlib HTTP, no SDKs. Placeholder/fallback used until keys are set.
- Invoice auto-numbering (INV-2026-001, QUO-… for quotations) + GST math computed
  server-side (`documents/services.py`) and injected into the prompt — the AI never
  invents figures. Invoices render a correct table even with no API key.
- Status transitions Draft → Review → Approved → Sent (with reopen), enforced
  server-side, buttons on the document page.
- Debounced autosave (2s) that reuses the current autosave version so history
  isn't flooded; "Save version" still creates explicit checkpoints.

## ✅ M5 — PDF export + branding  (DONE)
- "Export PDF" button renders `content_html` into a branded A4 shell
  (`documents/pdf.py`), stores a `GeneratedFile`, and downloads it.
- Engine-agnostic: WeasyPrint when its native libs exist (Linux servers),
  pure-Python xhtml2pdf fallback otherwise (works on Windows dev).
- Custom `Template` rows (html + css + is_default per type) are honored.
- DOCX export: still optional, not built.

## ✅ M6 — Document library  (DONE)
- Global Documents page (`/documents/`) + Projects page (`/projects/`): search across
  title/number/client/project, filter by type & status, archived view toggle.
- Duplicate (fresh INV/QUO number, content carried over), archive/restore (nothing
  deleted, hidden from lists).
- Version compare (side-by-side diff of any two versions) and "Restore" per version —
  restoring creates a new version so history is never rewritten.

## ✅ M7 — Ops layer  (DONE)
- Module permissions (`accounts/permissions.py`). Originally a hardcoded
  role→module map; now DB-driven — see "Dynamic RBAC" below.
- Activity log page (`/activity/`, paginated) + recent-activity card on dashboard.
- In-app notifications (topbar bell + `/notifications/`): review submissions ping
  approvers; approve/sent pings the document's creator.
- Agency settings screen (`/settings/`, owner-only): branding, contact, GSTIN,
  logo for PDF header; links to document types & AI prompts in admin.

## ✅ Visual Template Studio  (DONE — Admin → Templates, replaces the Design Asset Manager)
- The old `design_assets` app was removed (code backed up in
  `design_assets_removed_backup.zip`; uploaded files kept in `media/design_assets/`).
- A template is designed visually at `/templates/` — no HTML/CSS:
  cover image (page 1), content-page background (pages 2+), optional constant
  back/thank-you image (last page of every document), and ONE content region
  drawn by click-dragging a rectangle over an A4 preview. The region is stored
  as percentages of the page, so it is resolution-independent.
- Templates can be per document type or "any type", with one default each;
  new documents pick their default template automatically.
- PDF export renders the images for real on both engines (WeasyPrint named
  pages / xhtml2pdf page templates + frames): cover full-bleed, content flowing
  inside the drawn region over the background, back image as the final page.
  Documents without a template keep the clean branded A4 shell.
- Every document type takes a single "Brief" — the AI decides all structure.
  Writes owner-only; browsing for all roles.

## ✅ Dynamic RBAC  (DONE — `/settings/roles/`)
The hardcoded role→module map is gone. Permissions are now Role × Module ×
Action rows in the database (`accounts.Module` / `Role` / `RolePermission`),
edited in a matrix UI. `has_perm(user, module, action)`, `@require_perm(...)` and
`{% user_can %}` read from it. Adding a module is a catalog entry plus a seed
default. Nine roles ship seeded; Super Admin and Manager are system roles.

## ✅ Delivery workspace  (DONE — the project page)
Tasks (four-column board with review/approval), work log (the monthly-report
spine), assets, milestones, onboarding checklist, sprints, recurring tasks.
Finance, onboarding and documents demoted to compact summaries.

## ✅ Finance  (DONE)
`finance.Payment` = money received, decoupled from invoices. Project and client
rollups (value / received / outstanding). Visibility gated on `finance.view`,
which covers both money figures and financial document types.

## ✅ Employees  (DONE)
EmployeeProfile with skills and documents, sensitive salary/bank fields behind
`payroll.view`, self-service "My account", gated document downloads.

## ✅ Monthly reports  (DONE)
`generate_monthly_report()` builds the facts from work logs, completed tasks and
milestones, sends them through the AI, then *our* code appends the proof photos —
the model is never allowed to write image tags.

## ✅ Analytics  (DONE — `/analytics/`)
Five views (overview, employees, projects, clients, finance). Deliberately zero
models: every figure is derived at query time. Strict layering — `selectors.py`
is the only module touching the ORM, `services.py` does maths, `charts.py` emits
Chart.js, `exports.py` writes CSV and a dependency-free `.xlsx`.

## ✅ Resource planner  (DONE — `/planning/`)
Capacity profiles and leave, with allocation *derived* (logged hours + remaining
estimate) rather than stored. Weekly/monthly heatmap grid with drag-and-drop
reassignment that warns on overbooking instead of blocking.

## ✅ Unified calendar  (DONE — `/calendar/`)
An aggregator, not a second source of truth: seven adapters read the apps that
own the data (tasks, milestones, project deadlines, meetings, document reviews,
leave, birthdays). Only genuinely new tables are stored. Hand-written iCal
export; sync provider ABC in place, no third-party integration shipped.

## ✅ Login hardening + upload ceiling  (DONE)
Two-step sign-in — password, then a six-digit code emailed to the account. Codes
are stored as keyed hashes, never plaintext; failures are throttled in the DB.
`/admin/login/` is wrapped so it can't bypass the code. Uploads are capped
mid-stream, images re-encoded, oversized files pushed to a Drive link.

## ✅ Assignment emails  (DONE)
Being handed a task emails the assignee as well as ringing the in-app bell.
Skips self-assignment, unchanged assignees and addressless accounts; a dead mail
server can never fail the save.

## ✅ Row-level visibility  (DONE)
Membership decides what exists. A project is visible to its `members`; a task is
visible to its assignee, reviewer or creator, plus the unassigned backlog for
project members. The bypass is itself a permission (`view_all` on projects and
tasks, seeded to Super Admin and Manager) so it can be widened from the matrix.
Analytics and Planning are Manager-and-above, since both report across people.
Single source of truth: `projects/access.py`.

## ▶ M8 — Remaining
Client portal · e-signatures · CRM and time-tracking integrations · DOCX export.

> The `Client` role exists in the seed but is **not safe to assign**: no view
> scopes its queryset to the viewer's own client. It waits on the portal.

---

## Deploying
See **`DEPLOYMENT.md`** — production runbook for za-an.com on the shared BigRock
VPS (nginx + gunicorn + PostgreSQL, private GitHub repo, protected media).

---

### Where each piece lives
| Concern | Location |
|---|---|
| Add/edit document types | `documents/models.py::DocumentType` + admin, or `seed_doctypes` |
| AI generation | `ai_engine/services.py` (Groq, key rotation, stdlib HTTP) |
| Auto default project | `clients/signals.py` |
| The generate→edit→version flow | `documents/views.py` |
| Branding/PDF templates | `documents/models.py::Template` |
| Who may do what | `accounts/permissions.py` + `accounts/rbac_seed.py` |
| Who may see what | `projects/access.py`, `documents/access.py` |
| Outbound email | `accounts/security.py` (sign-in), `projects/emails.py` (assignment) |
| Scheduled jobs | `send_calendar_reminders`, `generate_recurring_tasks` (cron) |
