from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, Prefetch, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Department
from accounts.permissions import has_perm, require_perm, users_with_perm
from core.models import log_activity, notify
from core.tenancy import scope, workspace_for_new
from core.uploads import empty_upload_message, normalise_drive_link, process_upload
from clients.access import visible_clients
from clients.models import Client
from documents.access import visible_doc_types, visible_documents
from documents.models import DocumentType
from finance.models import Payment
from .access import (
    ensure_member, user_can_see_project, visible_projects, visible_tasks,
)
from .emails import send_project_member_added, send_task_assigned
from .models import (
    Asset, Milestone, OnboardingItem, Project, Sprint, Task, TaskAttachment,
    TaskChecklistItem, TaskComment, WorkLogEntry,
)

User = get_user_model()

# Rows rendered in the project page's work-log table. Totals are aggregated in
# the DB over the full filtered set, not over this slice.
WORKLOG_PAGE_SIZE = 200


@login_required
@require_perm("projects", "view")
def project_list(request):
    """Client-first browsing: every client as an accordion, its projects inside."""
    q = request.GET.get("q", "").strip()

    project_qs = visible_projects(
        Project.objects.filter(is_archived=False)
        .annotate(doc_count=Count("documents", filter=Q(documents__is_archived=False)))
        .prefetch_related("onboarding_items")
        .order_by("name"),
        request.user,
    )
    # The client accordion is driven off the projects the viewer can see, so a
    # client whose every project is hidden drops out of the page entirely
    # rather than sitting there as an empty row that leaks its existence.
    visible_ids = set(project_qs.values_list("pk", flat=True))
    clients = (
        Client.objects.filter(is_archived=False, projects__pk__in=visible_ids)
        .annotate(
            active_count=Count(
                "projects",
                filter=Q(projects__pk__in=visible_ids,
                         projects__is_archived=False,
                         projects__status=Project.Status.ACTIVE),
                distinct=True,
            )
        )
        .prefetch_related(Prefetch("projects", queryset=project_qs,
                                   to_attr="visible_projects"))
        .order_by("name")
        .distinct()
    )
    if q:
        clients = clients.filter(
            Q(name__icontains=q)
            | Q(projects__name__icontains=q, projects__is_archived=False,
                projects__pk__in=visible_ids)
        ).distinct()

    clients = list(clients)

    # Per-client outstanding = Σ budgets − Σ payments (single-currency INR).
    # Two separate grouped aggregates: annotating both Sums on the client
    # queryset would fan out across the projects×payments join.
    # Scoped to the same visible set, or the totals would describe projects the
    # viewer cannot open — a money figure is a leak like any other.
    value_by_client = {
        row["client"]: row["s"] or Decimal("0")
        for row in Project.objects.filter(is_archived=False, pk__in=visible_ids)
        .values("client").annotate(s=Sum("budget"))
    }
    received_by_client = {
        row["project__client"]: row["s"] or Decimal("0")
        for row in Payment.objects.filter(project__is_archived=False,
                                          project__pk__in=visible_ids)
        .values("project__client").annotate(s=Sum("amount"))
    }
    for c in clients:
        c.fin_value = value_by_client.get(c.pk, Decimal("0"))
        c.fin_outstanding = c.fin_value - received_by_client.get(c.pk, Decimal("0"))

    return render(request, "projects/list.html", {"clients": clients, "q": q})


@login_required
@require_perm("projects", "create")
def project_create(request, client_pk=None):
    """Create a project. Works both from a client page (client fixed via URL)
    and standalone at /projects/new/ (client chosen in the form)."""
    # Both client lookups run through the workspace filter, so a hand-typed
    # client id from another partition 404s rather than silently parenting a
    # project onto somebody else's client.
    client_qs = visible_clients(Client.objects.all(), request.user)
    client = get_object_or_404(client_qs, pk=client_pk) if client_pk else None
    clients = None
    if client is None:
        clients = client_qs.filter(is_archived=False).order_by("name")

    if request.method == "POST":
        if client is None:
            client = get_object_or_404(
                client_qs, pk=request.POST.get("client"), is_archived=False
            )
        project = Project.objects.create(
            client=client,
            name=request.POST["name"],
            project_type=request.POST.get("project_type", ""),
            description=request.POST.get("description", ""),
            created_by=request.user,
            # The client's partition wins over the creator's active one: a
            # project belongs where its client lives, and the two only differ
            # if a home admin is creating inside a partner's workspace.
            workspace_id=client.workspace_id or workspace_for_new(request.user).pk,
        )
        log_activity(request.user, "created project", project.name)
        return redirect(project)

    return render(request, "projects/form.html", {"client": client, "clients": clients})


@login_required
@require_perm("projects", "view")
def project_detail(request, pk):
    """The delivery workspace: overview, tasks, work log, assets, milestones,
    with finances/onboarding/documents demoted to compact summaries."""
    project = get_object_or_404(
        visible_projects(
            Project.objects.select_related("client").prefetch_related(
                Prefetch("tasks", queryset=_task_board_queryset(request.user))
            ),
            request.user,
        ),
        pk=pk,
    )
    # Backfill for pre-feature projects, then reconcile the auto item with
    # finance data (belt and suspenders alongside the Payment signals).
    project.ensure_onboarding()
    project.sync_onboarding()

    tasks = list(project.tasks.all())
    columns = {"todo": [], "in_progress": [], "submitted": [], "done": []}
    for t in tasks:
        columns[t.board_column].append(t)

    worklogs, worklog_filters = _filtered_worklogs(request, project)

    assets = list(project.assets.select_related("uploaded_by"))
    asset_groups = []
    for value, label in Asset.Category.choices:
        if value == Asset.Category.CREDENTIAL:
            continue
        group = [a for a in assets if a.category == value]
        if group:
            asset_groups.append((label, group))

    return render(request, "projects/detail.html", {
        "project": project,
        "tasks_todo": columns["todo"],
        "tasks_progress": columns["in_progress"],
        "tasks_submitted": columns["submitted"],
        "tasks_done": columns["done"],
        "task_summary": project.task_summary,
        "task_statuses": Task.Status.choices,
        "task_priorities": Task.Priority.choices,
        "team": _team(),
        # select_related because the roster prints each person's designation,
        # whose __str__ reaches on into department — two extra queries per row
        # otherwise.
        "members": project.members.filter(is_active=True)
                          .select_related("designation__department")
                          .order_by("first_name", "username"),
        "addable_people": _team().exclude(
            pk__in=project.members.values("pk")),
        "can_manage_members": has_perm(request.user, "projects", "edit"),
        "departments": Department.objects.all(),
        "sprints": project.sprints.filter(is_active=True),
        "work_logs": worklogs,
        "worklog_filters": worklog_filters,
        "worklog_summary": WorkLogEntry.project_summary(project),
        "worklog_filtered_total": worklog_filters["total"],
        "worklog_categories": WorkLogEntry.Category.choices,
        "worklog_approval_choices": WorkLogEntry.Approval.choices,
        "open_tasks": [t for t in tasks if t.status != Task.Status.DONE],
        "asset_groups": asset_groups,
        "credential_assets": [a for a in assets
                              if a.category == Asset.Category.CREDENTIAL],
        "asset_categories": Asset.Category.choices,
        "milestones": project.milestones.all(),
        "doc_count": visible_documents(
            project.documents.filter(is_archived=False), request.user).count(),
        "doc_types": visible_doc_types(
            DocumentType.objects.filter(is_active=True), request.user),
        "onboarding": project.onboarding_progress,
        "today": timezone.localdate(),
    })


def _filtered_worklogs(request, project):
    """Work logs for the project detail page, narrowed by the filter bar.

    Returns (entries, filter_state) — the state is echoed back into the form so
    the controls keep showing what's applied.
    """
    entries = (project.work_logs
               .select_related("logged_by", "task", "approved_by")
               .order_by("-date", "-created_at"))

    state = {
        "member": request.GET.get("member", ""),
        "billable": request.GET.get("billable", ""),
        "approval": request.GET.get("approval", ""),
        "from": request.GET.get("from", ""),
        "to": request.GET.get("to", ""),
    }

    if state["member"].isdigit():
        entries = entries.filter(logged_by_id=int(state["member"]))
    if state["billable"] in ("yes", "no"):
        entries = entries.filter(is_billable=state["billable"] == "yes")
    if state["approval"] in WorkLogEntry.Approval.values:
        entries = entries.filter(approval_status=state["approval"])

    date_from = _parse_date(state["from"])
    if date_from:
        entries = entries.filter(date__gte=date_from)
    date_to = _parse_date(state["to"])
    if date_to:
        entries = entries.filter(date__lte=date_to)

    state["active"] = any(v for k, v in state.items() if k != "active")
    # Members who actually logged here — a filter listing the whole company
    # would be mostly dead options.
    state["members"] = (User.objects
                        .filter(worklogentry__project=project)
                        .distinct().order_by("first_name", "username"))
    # Totals come from the DB across the WHOLE filtered set; the row list is
    # capped so the page stays bounded. Summing the capped list instead would
    # under-report the moment a project passes 200 entries.
    state["total"] = entries.aggregate(s=Sum("hours"))["s"] or Decimal("0")
    state["truncated"] = entries.count() > WORKLOG_PAGE_SIZE
    return list(entries[:WORKLOG_PAGE_SIZE]), state


@login_required
@require_perm("projects", "edit")
@require_POST
def project_members(request, pk):
    """Add or remove one person from the project team.

    Membership is what makes the project visible at all, so this is a real
    permission change rather than a label — it is gated on `projects.edit` and
    written to the activity log like any other.
    """
    project = get_object_or_404(
        visible_projects(Project.objects.all(), request.user), pk=pk)
    user_id = _pk_or_none(request.POST.get("user"))
    person = User.objects.filter(pk=user_id, is_active=True).first() if user_id else None
    if person is None:
        messages.error(request, "Pick someone to add or remove.")
        return redirect(f"{project.get_absolute_url()}#team")

    if request.POST.get("action") == "remove":
        project.members.remove(person)
        log_activity(request.user, "removed project member",
                     f"{person.get_full_name() or person.username} · {project.name}")
        messages.success(
            request,
            f"{person.get_full_name() or person.username} can no longer see this "
            "project. Tasks assigned to them are untouched.")
    else:
        added = ensure_member(project, person)
        log_activity(request.user, "added project member",
                     f"{person.get_full_name() or person.username} · {project.name}")
        if added:
            # Only on a real addition. Re-submitting the form for someone who is
            # already on the project must not mail them a second time — the
            # activity log records the click either way.
            notify([person], f"You were added to “{project.name}”",
                   project.get_absolute_url(), exclude=request.user)
            send_project_member_added(project, person,
                                      actor=request.user, request=request)
        messages.success(
            request,
            f"{person.get_full_name() or person.username} added to the project."
            if added else "They were already on this project.")
    return redirect(f"{project.get_absolute_url()}#team")


@login_required
@require_perm("projects", "edit")
def project_edit(request, pk):
    """Edit the Overview fields: status, type, description, dates, value, links."""
    project = get_object_or_404(
        visible_projects(Project.objects.select_related("client"), request.user), pk=pk)
    if request.method == "POST":
        project.name = request.POST.get("name", project.name).strip() or project.name
        project.project_type = request.POST.get("project_type", "")
        project.description = request.POST.get("description", "")
        status = request.POST.get("status", "")
        if status in Project.Status.values:
            project.status = status
        # Parsed rather than assigned raw: a non-numeric budget or a malformed
        # date used to reach the DB layer and 500 the request.
        budget_raw = (request.POST.get("budget") or "").strip()
        budget = _decimal_or_none(budget_raw)
        if budget_raw and budget is None:
            messages.error(request,
                           f"Couldn't read “{budget_raw}” as an amount — project "
                           "value left unchanged.")
        else:
            project.budget = budget
        project.start_date = _parse_date(request.POST.get("start_date"))
        project.target_end_date = _parse_date(request.POST.get("target_end_date"))
        for field in ("live_url", "staging_url", "repo_url", "assets_url"):
            setattr(project, field, request.POST.get(field, "").strip())
        project.save()
        log_activity(request.user, "updated project", project.name)
        messages.success(request, "Project updated.")
        return redirect(project)
    return render(request, "projects/edit.html", {
        "project": project,
        "statuses": Project.Status.choices,
    })


@login_required
@require_perm("projects", "delete")
@require_POST
def project_delete(request, pk):
    project = get_object_or_404(
        visible_projects(Project.objects.select_related("client"), request.user), pk=pk)
    name, client = project.name, project.client
    project.delete()
    log_activity(request.user, "deleted project", f"{name} · {client.name}")
    messages.success(request, f"“{name}” deleted, along with its documents.")
    return redirect("projects:list")


@login_required
@require_perm("projects", "delete")
@require_POST
def project_bulk_delete(request):
    projects = list(
        Project.objects.filter(pk__in=request.POST.getlist("selected"))
        .select_related("client")
    )
    if not projects:
        messages.error(request, "No projects selected.")
        return redirect("projects:list")
    for p in projects:
        log_activity(request.user, "deleted project", f"{p.name} · {p.client.name}")
    Project.objects.filter(pk__in=[p.pk for p in projects]).delete()
    messages.success(
        request,
        f"Deleted {len(projects)} project{'s' if len(projects) != 1 else ''}, "
        "along with their documents.",
    )
    return redirect("projects:list")


# =====================================================================
# tasks
# =====================================================================
#
# Two permission questions run through every task view, and they are NOT the
# same as the module-level RBAC check:
#
#   _can_edit(user, task)   tasks.edit  OR being the assignee
#   _can_review(user, task) tasks.approve OR being the named reviewer
#
# The module actions come from dynamic RBAC (accounts.permissions); the
# per-object clauses let an individual contributor drive their own task and
# a nominated reviewer sign it off without holding blanket permissions.


def _team():
    return User.objects.filter(is_active=True).order_by("first_name", "username")


def _task_board_queryset(user=None):
    """Everything the board and My Tasks cards read, without N+1s.

    `user` is required at every user-facing call site — it is what narrows the
    board to the viewer's own tasks. The default of None means system context
    and returns everything; see projects/access.py.
    """
    qs = (Task.objects
          .select_related("assignee", "reviewer", "department", "project")
          .prefetch_related("checklist", "blocked_by"))
    return visible_tasks(qs, user)


def _can_edit(user, task):
    return has_perm(user, "tasks", "edit") or task.assignee_id == user.pk


def _can_review(user, task):
    return has_perm(user, "tasks", "approve") or task.reviewer_id == user.pk


def _decimal_or_none(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def _pk_or_none(raw):
    """POST values are strings, and `"3" != 3` is True.

    Assigning `request.POST["assignee"]` straight onto `task.assignee_id` leaves
    a string on the instance, so every later comparison against the previous
    (integer) id reports a change that did not happen — which re-notifies and
    re-emails the same person each time the form is saved.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _get_task(task_pk, user):
    """Fetch a task the viewer is allowed to see, else 404.

    Every task view goes through here, which is what makes row-level visibility
    hold on the write paths too — several of them (task_set_status, task_submit,
    checklist_*, attachment_*) carry no module decorator at all and would
    otherwise let anyone with tasks.edit drive a stranger's task by URL.
    """
    return get_object_or_404(
        _task_board_queryset(user).select_related("project__client"), pk=task_pk
    )


def _blocker_message(task):
    """The refusal text when open blockers stop a completion, or None."""
    blockers = task.open_blockers
    if not blockers:
        return None
    names = ", ".join(f"“{b.title}”" for b in blockers[:3])
    extra = f" (+{len(blockers) - 3} more)" if len(blockers) > 3 else ""
    return (f"“{task.title}” is blocked by {names}{extra}. "
            "Finish the blocking task(s) first.")


@login_required
@require_perm("tasks", "create")
@require_POST
def task_create(request, pk):
    project = get_object_or_404(visible_projects(Project.objects.all(), request.user), pk=pk)
    title = request.POST.get("title", "").strip()
    if not title:
        messages.error(request, "Task title is required.")
        return redirect(project)
    priority = request.POST.get("priority", Task.Priority.MED)
    task = Task.objects.create(
        project=project,
        title=title,
        priority=priority if priority in Task.Priority.values else Task.Priority.MED,
        assignee_id=_pk_or_none(request.POST.get("assignee")),
        reviewer_id=_pk_or_none(request.POST.get("reviewer")),
        department_id=_pk_or_none(request.POST.get("department")),
        due_date=_parse_date(request.POST.get("due_date")),
        estimated_hours=_decimal_or_none(request.POST.get("estimated_hours")),
        created_by=request.user,
    )
    log_activity(request.user, "added task", f"{title} · {project.name}")
    if task.assignee:
        notify([task.assignee], f"You were assigned: {task.title}",
               url=task.get_absolute_url(), exclude=request.user)
        # Being handed work on a project you cannot open is a dead end: the
        # email and the bell would both point at a 404. Assignment therefore
        # grants membership.
        ensure_member(task.project, task.assignee)
        send_task_assigned(task, actor=request.user, request=request)
    return redirect(project)


@login_required
def task_detail(request, task_pk):
    """Everything about one task on a single page."""
    task = get_object_or_404(
        visible_tasks(
            Task.objects
            .select_related("project", "project__client", "assignee", "reviewer",
                            "department", "sprint", "approved_by", "created_by")
            .prefetch_related("checklist", "comments__author",
                              "attachments__uploaded_by",
                              "blocked_by__project", "blocks__project"),
            request.user,
        ),
        pk=task_pk,
    )
    if not has_perm(request.user, "tasks", "view"):
        messages.error(request, "You don't have 'view' permission on tasks.")
        return redirect("core:dashboard")

    can_edit = _can_edit(request.user, task)
    task_logs = list(
        task.work_logs.select_related("logged_by", "approved_by")
        .order_by("-date", "-created_at")
    )
    logged_total = sum((w.hours or Decimal("0")) for w in task_logs)
    billable_total = sum((w.hours or Decimal("0")) for w in task_logs if w.is_billable)

    return render(request, "projects/task_detail.html", {
        "task": task,
        "project": task.project,
        "task_logs": task_logs,
        "logged_total": logged_total,
        "billable_total": billable_total,
        "worklog_categories": WorkLogEntry.Category.choices,
        "can_log_work": _worklog_module_access(request.user),
        "can_edit": can_edit,
        "can_assign": has_perm(request.user, "tasks", "assign"),
        "can_review": _can_review(request.user, task),
        "can_delete": has_perm(request.user, "tasks", "delete"),
        "is_assignee": task.assignee_id == request.user.pk,
        "open_blockers": task.open_blockers,
        "blocker_message": _blocker_message(task),
        "statuses": Task.Status.choices,
        "priorities": Task.Priority.choices,
        "team": _team(),
        "departments": Department.objects.all(),
        "sprints": Sprint.objects.filter(project=task.project, is_active=True),
        "candidate_blockers": (
            visible_tasks(Task.objects.filter(project=task.project), request.user)
            .exclude(pk=task.pk).order_by("status", "title")
        ),
        "today": timezone.localdate(),
    })


@login_required
@require_POST
def task_set_status(request, task_pk):
    """Move a task between columns.

    Completion is the interesting case: a task with a reviewer submits for
    review instead of finishing, and open blockers refuse the move outright.
    """
    task = _get_task(task_pk, request.user)
    if not _can_edit(request.user, task):
        messages.error(request, "You can't change this task's status.")
        return redirect(request.POST.get("next") or task.get_absolute_url())

    status = request.POST.get("status", "")
    if status not in Task.Status.values:
        return redirect(request.POST.get("next") or task.get_absolute_url())

    redirect_to = request.POST.get("next") or task.get_absolute_url()

    if status == Task.Status.DONE:
        blocked = _blocker_message(task)
        if blocked:
            messages.error(request, blocked)
            return redirect(redirect_to)
        if task.needs_review:
            _submit_for_review(request, task)
            return redirect(redirect_to)
        task.status = Task.Status.DONE
        task.completed_on = timezone.localdate()
        task.approval_status = Task.Approval.NONE
        task.save(update_fields=["status", "completed_on", "approval_status",
                                 "updated_at"])
        log_activity(request.user, "completed task",
                     f"{task.title} · {task.project.name}")
        messages.success(request, f"“{task.title}” marked done.")
        return redirect(redirect_to)

    # Moving back to TODO / IN_PROGRESS clears the completion date and the
    # whole review state — a pending submission AND a past sign-off. Work that
    # is live again is not approved work, so the stamp goes with it (this is
    # what task_reopen already does).
    task.status = status
    task.completed_on = None
    _clear_review_state(task)
    task.save(update_fields=["status", "completed_on", "approval_status",
                             "approved_by", "approved_on", "updated_at"])
    return redirect(redirect_to)


def _clear_review_state(task):
    """Drop any submission or sign-off from a task that's back in play."""
    if task.approval_status != Task.Approval.REOPENED:
        task.approval_status = Task.Approval.NONE
    task.approved_by = None
    task.approved_on = None


def _submit_for_review(request, task):
    """Shared by task_set_status(DONE) and the explicit Submit button."""
    task.approval_status = Task.Approval.SUBMITTED
    task.status = Task.Status.IN_PROGRESS
    task.completed_on = None
    task.save(update_fields=["approval_status", "status", "completed_on",
                             "updated_at"])
    log_activity(request.user, "submitted task for review",
                 f"{task.title} · {task.project.name}")
    if task.reviewer:
        notify([task.reviewer], f"Review requested: {task.title}",
               url=task.get_absolute_url(), exclude=request.user)
    messages.success(
        request,
        f"“{task.title}” sent to "
        f"{task.reviewer.get_full_name() or task.reviewer.username} for review."
        if task.reviewer else f"“{task.title}” submitted for review.")


@login_required
@require_POST
def task_submit(request, task_pk):
    """Assignee (or an editor) hands the task to its reviewer."""
    task = _get_task(task_pk, request.user)
    if not _can_edit(request.user, task):
        messages.error(request, "You can't submit this task.")
        return redirect(task.get_absolute_url())

    redirect_to = request.POST.get("next") or task.get_absolute_url()
    blocked = _blocker_message(task)
    if blocked:
        messages.error(request, blocked)
        return redirect(redirect_to)
    if not task.needs_review:
        messages.error(
            request, "This task has no reviewer — mark it done directly.")
        return redirect(redirect_to)

    _submit_for_review(request, task)
    return redirect(redirect_to)


@login_required
@require_POST
def task_approve(request, task_pk):
    """Reviewer signs off: the task becomes DONE here and nowhere else."""
    task = _get_task(task_pk, request.user)
    if not _can_review(request.user, task):
        messages.error(request, "Only the reviewer can approve this task.")
        return redirect(task.get_absolute_url())

    redirect_to = request.POST.get("next") or task.get_absolute_url()
    if not task.is_awaiting_review:
        messages.error(request, "This task isn't awaiting review.")
        return redirect(redirect_to)

    blocked = _blocker_message(task)
    if blocked:
        messages.error(request, blocked)
        return redirect(redirect_to)

    task.approval_status = Task.Approval.APPROVED
    task.status = Task.Status.DONE
    task.completed_on = timezone.localdate()
    task.approved_by = request.user
    task.approved_on = timezone.now()
    task.save(update_fields=["approval_status", "status", "completed_on",
                             "approved_by", "approved_on", "updated_at"])

    note = request.POST.get("comment", "").strip()
    if note:
        TaskComment.objects.create(task=task, author=request.user,
                                   body=f"Approved: {note}")

    log_activity(request.user, "approved task",
                 f"{task.title} · {task.project.name}")
    if task.assignee:
        notify([task.assignee], f"Approved: {task.title}",
               url=task.get_absolute_url(), exclude=request.user)
    messages.success(request, f"“{task.title}” approved and marked done.")
    return redirect(redirect_to)


@login_required
@require_POST
def task_reopen(request, task_pk):
    """Reviewer sends it back. A reason is required — that's the whole point."""
    task = _get_task(task_pk, request.user)
    if not _can_review(request.user, task):
        messages.error(request, "Only the reviewer can reopen this task.")
        return redirect(task.get_absolute_url())

    redirect_to = request.POST.get("next") or task.get_absolute_url()
    reason = request.POST.get("comment", "").strip()
    if not reason:
        messages.error(request, "Add a reason so the assignee knows what to fix.")
        return redirect(redirect_to)

    task.approval_status = Task.Approval.REOPENED
    task.status = Task.Status.IN_PROGRESS
    task.completed_on = None
    task.approved_by = None
    task.approved_on = None
    task.save(update_fields=["approval_status", "status", "completed_on",
                             "approved_by", "approved_on", "updated_at"])

    TaskComment.objects.create(task=task, author=request.user,
                               body=f"Reopened: {reason}")
    log_activity(request.user, "reopened task",
                 f"{task.title} · {task.project.name}")
    if task.assignee:
        notify([task.assignee], f"Reopened: {task.title} — {reason[:80]}",
               url=task.get_absolute_url(), exclude=request.user)
    messages.success(request, f"“{task.title}” sent back to the assignee.")
    return redirect(redirect_to)


@login_required
def task_edit(request, task_pk):
    """Full-field edit. Assignee changes need tasks.assign on top of edit rights."""
    task = _get_task(task_pk, request.user)
    if not _can_edit(request.user, task):
        messages.error(request, "You don't have permission to edit this task.")
        return redirect(task.get_absolute_url())

    can_assign = has_perm(request.user, "tasks", "assign")

    if request.method == "POST":
        task.title = request.POST.get("title", task.title).strip() or task.title
        task.description = request.POST.get("description", "")

        priority = request.POST.get("priority", "")
        if priority in Task.Priority.values:
            task.priority = priority

        task.due_date = _parse_date(request.POST.get("due_date"))
        task.department_id = request.POST.get("department") or None
        task.sprint_id = request.POST.get("sprint") or None
        if "estimated_hours" in request.POST:
            raw = request.POST["estimated_hours"].strip()
            parsed = _decimal_or_none(raw)
            if raw and parsed is None:
                # Unreadable is not the same as cleared: keep the old estimate
                # rather than silently wiping it on a typo.
                messages.error(request,
                               f"Couldn't read “{raw}” as hours — estimate left "
                               "unchanged.")
            else:
                task.estimated_hours = parsed
        task.is_billable = bool(request.POST.get("is_billable"))

        # Assignment is a separate right: an assignee may edit their own task
        # but not hand it off, and may not nominate their own reviewer.
        old_assignee_id = task.assignee_id
        if can_assign:
            task.assignee_id = _pk_or_none(request.POST.get("assignee"))
            task.reviewer_id = _pk_or_none(request.POST.get("reviewer"))
        elif ("assignee" in request.POST
                and str(request.POST["assignee"] or "") != str(old_assignee_id or "")):
            # The form disables these selects, so a mismatch here means a
            # hand-crafted POST rather than a stale tab.
            messages.error(request, "You don't have 'assign' permission on tasks.")

        status = request.POST.get("status", "")
        if status in Task.Status.values and status != task.status:
            if status == Task.Status.DONE:
                blocked = _blocker_message(task)
                if blocked:
                    messages.error(request, blocked)
                elif task.needs_review:
                    task.status = Task.Status.IN_PROGRESS
                    task.approval_status = Task.Approval.SUBMITTED
                    task.completed_on = None
                    if task.reviewer:
                        notify([task.reviewer], f"Review requested: {task.title}",
                               url=task.get_absolute_url(), exclude=request.user)
                    messages.info(request, "Sent for review instead of completing.")
                else:
                    task.status = Task.Status.DONE
                    task.completed_on = timezone.localdate()
            else:
                task.status = status
                task.completed_on = None
                _clear_review_state(task)

        task.save()

        if task.assignee_id and task.assignee_id != old_assignee_id:
            notify([task.assignee], f"You were assigned: {task.title}",
                   url=task.get_absolute_url(), exclude=request.user)
            # Being handed work on a project you cannot open is a dead end: the
        # email and the bell would both point at a 404. Assignment therefore
        # grants membership.
        ensure_member(task.project, task.assignee)
        send_task_assigned(task, actor=request.user, request=request)

        log_activity(request.user, "updated task",
                     f"{task.title} · {task.project.name}")
        messages.success(request, "Task updated.")
        return redirect(task.get_absolute_url())

    return render(request, "projects/task_form.html", {
        "task": task,
        "project": task.project,
        "statuses": Task.Status.choices,
        "priorities": Task.Priority.choices,
        "team": _team(),
        "departments": Department.objects.all(),
        "sprints": Sprint.objects.filter(project=task.project, is_active=True),
        "can_assign": can_assign,
    })


@login_required
@require_perm("tasks", "assign")
@require_POST
def task_assign(request, task_pk):
    """Quick assignee/reviewer swap from the task detail header."""
    task = _get_task(task_pk, request.user)
    old_assignee_id = task.assignee_id
    task.assignee_id = _pk_or_none(request.POST.get("assignee"))
    task.reviewer_id = _pk_or_none(request.POST.get("reviewer"))
    task.save(update_fields=["assignee", "reviewer", "updated_at"])

    if task.assignee_id and task.assignee_id != old_assignee_id:
        notify([task.assignee], f"You were assigned: {task.title}",
               url=task.get_absolute_url(), exclude=request.user)
        # Being handed work on a project you cannot open is a dead end: the
        # email and the bell would both point at a 404. Assignment therefore
        # grants membership.
        ensure_member(task.project, task.assignee)
        send_task_assigned(task, actor=request.user, request=request)
    log_activity(request.user, "reassigned task",
                 f"{task.title} · {task.project.name}")
    messages.success(request, "Assignment updated.")
    return redirect(task.get_absolute_url())


@login_required
@require_perm("tasks", "delete")
@require_POST
def task_delete(request, task_pk):
    # `_get_task`, like every other task view: this one used to fetch by bare
    # primary key, so `tasks.delete` was enough to destroy a task in a project
    # — or a workspace — the caller could not otherwise see.
    task = _get_task(task_pk, request.user)
    project = task.project
    task.delete()
    messages.success(request, "Task deleted.")
    return redirect(project)


# --- task dependencies ---

@login_required
@require_POST
def task_deps_update(request, task_pk):
    """Replace the blocker set. Self-blocks and cross-project picks are dropped."""
    task = _get_task(task_pk, request.user)
    if not _can_edit(request.user, task):
        messages.error(request, "You can't change this task's dependencies.")
        return redirect(task.get_absolute_url())

    picked = (Task.objects
              .filter(pk__in=request.POST.getlist("blocked_by"),
                      project=task.project)
              .exclude(pk=task.pk))
    task.blocked_by.set(picked)
    messages.success(request, "Dependencies updated.")
    return redirect(task.get_absolute_url())


# --- checklist ---

@login_required
@require_POST
def checklist_add(request, task_pk):
    task = _get_task(task_pk, request.user)
    if not _can_edit(request.user, task):
        messages.error(request, "You can't edit this checklist.")
        return redirect(task.get_absolute_url())
    text = request.POST.get("text", "").strip()
    if not text:
        messages.error(request, "Checklist item needs some text.")
        return redirect(task.get_absolute_url())
    TaskChecklistItem.objects.create(
        task=task, text=text,
        order=task.checklist.count(),
    )
    return redirect(task.get_absolute_url())


@login_required
@require_POST
def checklist_toggle(request, item_pk):
    item = get_object_or_404(
        scope(TaskChecklistItem.objects.select_related("task"),
              request.user, path="task__project__workspace"), pk=item_pk)
    if not _can_edit(request.user, item.task):
        messages.error(request, "You can't edit this checklist.")
        return redirect(item.task.get_absolute_url())
    item.is_done = not item.is_done
    item.save(update_fields=["is_done", "updated_at"])
    return redirect(item.task.get_absolute_url())


@login_required
@require_POST
def checklist_move(request, item_pk):
    """Swap an item with its neighbour — reorder without drag-and-drop."""
    item = get_object_or_404(
        scope(TaskChecklistItem.objects.select_related("task"),
              request.user, path="task__project__workspace"), pk=item_pk)
    if not _can_edit(request.user, item.task):
        messages.error(request, "You can't reorder this checklist.")
        return redirect(item.task.get_absolute_url())

    items = list(item.task.checklist.all())
    index = next((i for i, x in enumerate(items) if x.pk == item.pk), None)
    step = -1 if request.POST.get("dir") == "up" else 1
    target = index + step if index is not None else None

    if target is not None and 0 <= target < len(items):
        other = items[target]
        items[index], items[target] = items[target], items[index]
        # Rewrite the whole run: legacy rows can share order values, so
        # swapping just these two wouldn't always reorder anything.
        for position, row in enumerate(items):
            row.order = position
        TaskChecklistItem.objects.bulk_update(items, ["order"])
    return redirect(item.task.get_absolute_url())


@login_required
@require_POST
def checklist_delete(request, item_pk):
    item = get_object_or_404(
        scope(TaskChecklistItem.objects.select_related("task"),
              request.user, path="task__project__workspace"), pk=item_pk)
    if not _can_edit(request.user, item.task):
        messages.error(request, "You can't edit this checklist.")
        return redirect(item.task.get_absolute_url())
    task = item.task
    item.delete()
    return redirect(task.get_absolute_url())


# --- comments ---

@login_required
@require_POST
def comment_add(request, task_pk):
    """Anyone who can see tasks can comment — that's the point of a thread."""
    task = _get_task(task_pk, request.user)
    if not has_perm(request.user, "tasks", "view"):
        messages.error(request, "You don't have 'view' permission on tasks.")
        return redirect("core:dashboard")

    body = request.POST.get("body", "").strip()
    if not body:
        messages.error(request, "Write something first.")
        return redirect(task.get_absolute_url())

    TaskComment.objects.create(task=task, author=request.user, body=body)

    # Ping the other side of the task, never the author.
    watchers = [u for u in (task.assignee, task.reviewer) if u]
    if watchers:
        notify(watchers, f"New comment on: {task.title}",
               url=task.get_absolute_url(), exclude=request.user)
    return redirect(task.get_absolute_url())


@login_required
@require_POST
def comment_delete(request, comment_pk):
    comment = get_object_or_404(
        scope(TaskComment.objects.select_related("task"),
              request.user, path="task__project__workspace"), pk=comment_pk)
    if not (comment.author_id == request.user.pk
            or has_perm(request.user, "tasks", "delete")):
        messages.error(request, "You can only delete your own comments.")
        return redirect(comment.task.get_absolute_url())
    task = comment.task
    comment.delete()
    return redirect(task.get_absolute_url())


# --- attachments ---

@login_required
@require_POST
def attachment_upload(request, task_pk):
    task = _get_task(task_pk, request.user)
    if not _can_edit(request.user, task):
        messages.error(request, "You can't add attachments to this task.")
        return redirect(task.get_absolute_url())
    upload = request.FILES.get("file")
    link = request.POST.get("external_url", "").strip()

    if link and not upload:
        try:
            link = normalise_drive_link(link)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect(task.get_absolute_url())
        TaskAttachment.objects.create(
            task=task, external_url=link,
            label=request.POST.get("label", "").strip(), uploaded_by=request.user)
        log_activity(request.user, "linked file on task",
                     f"{task.title} · {task.project.name}")
        return redirect(task.get_absolute_url())

    if not upload:
        # An oversized file never reaches here: CappedFileUploadHandler aborts
        # the transfer, leaving request.FILES empty. Say so, or the user sees
        # "choose a file" after clearly having chosen one.
        messages.error(request, empty_upload_message())
        return redirect(task.get_absolute_url())
    try:
        upload = process_upload(upload)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect(task.get_absolute_url())
    TaskAttachment.objects.create(task=task, file=upload, uploaded_by=request.user)
    log_activity(request.user, "attached file to task",
                 f"{task.title} · {task.project.name}")
    return redirect(task.get_absolute_url())


@login_required
@require_POST
def attachment_delete(request, attachment_pk):
    attachment = get_object_or_404(
        scope(TaskAttachment.objects.select_related("task"),
              request.user, path="task__project__workspace"), pk=attachment_pk)
    if not _can_edit(request.user, attachment.task):
        messages.error(request, "You can't remove this attachment.")
        return redirect(attachment.task.get_absolute_url())
    task = attachment.task
    attachment.file.delete(save=False)
    attachment.delete()
    return redirect(task.get_absolute_url())


# --- my tasks ---

@login_required
def my_tasks(request):
    """Everything on the logged-in user's plate, across every project."""
    if not has_perm(request.user, "tasks", "view"):
        messages.error(request, "You don't have 'view' permission on tasks.")
        return redirect("core:dashboard")

    today = timezone.localdate()
    mine = (_task_board_queryset(request.user)
            .filter(assignee=request.user)
            .select_related("project__client"))

    columns = {"todo": [], "in_progress": [], "submitted": [], "done": []}
    overdue_count = 0
    for t in mine:
        columns[t.board_column].append(t)
        if t.is_overdue:
            overdue_count += 1

    to_review = (_task_board_queryset(request.user)
                 .filter(reviewer=request.user,
                         approval_status=Task.Approval.SUBMITTED)
                 .select_related("project__client"))

    return render(request, "projects/my_tasks.html", {
        "tasks_todo": columns["todo"],
        "tasks_progress": columns["in_progress"],
        "tasks_submitted": columns["submitted"],
        # Finished work is reference material, not a to-do list: newest 20 only.
        "tasks_done": columns["done"][:20],
        "done_total": len(columns["done"]),
        "to_review": list(to_review),
        "overdue_count": overdue_count,
        "open_count": (len(columns["todo"]) + len(columns["in_progress"])
                       + len(columns["submitted"])),
        "today": today,
    })


# =====================================================================
# work log / timesheet
# =====================================================================
#
# Ownership beats module rights here. Everyone inside the worklogs module can
# create/read/update/delete THEIR OWN entries; touching someone else's needs
# worklogs.edit (or .delete), and approving needs worklogs.approve or being
# the logger's manager via reports_to.
#
# "Everyone" is scoped to holders of worklogs.view — that is what makes the
# module reachable at all, and keeps read-only roles (e.g. Client) out.


def _worklog_module_access(user):
    return has_perm(user, "worklogs", "view")


def _owns(user, entry):
    return entry.logged_by_id == user.pk


def _can_edit_log(user, entry):
    return _owns(user, entry) or has_perm(user, "worklogs", "edit")


def _can_delete_log(user, entry):
    return _owns(user, entry) or has_perm(user, "worklogs", "delete")


def _can_approve_log(user, entry):
    """Approvers are permission holders or the logger's line manager."""
    if has_perm(user, "worklogs", "approve"):
        return True
    logger = entry.logged_by
    return bool(logger and logger.reports_to_id == user.pk)


def _is_any_approver(user):
    """Whether to show the approvals queue at all."""
    if has_perm(user, "worklogs", "approve"):
        return True
    return User.objects.filter(reports_to=user).exists()


def _worklog_fields_from_post(request):
    """Shared parsing for create and edit. Returns (values, error)."""
    description = request.POST.get("description", "").strip()
    entry_date = request.POST.get("date") or None
    if not description or not entry_date:
        return None, "A work log needs a date and a description."

    start_time, start_ok = _parse_time(request.POST.get("start_time"))
    end_time, end_ok = _parse_time(request.POST.get("end_time"))
    if not (start_ok and end_ok):
        return None, "Start and end must be valid times (HH:MM)."

    category = request.POST.get("category", WorkLogEntry.Category.DEV)
    values = {
        "date": entry_date,
        "description": description,
        "category": (category if category in WorkLogEntry.Category.values
                     else WorkLogEntry.Category.OTHER),
        "start_time": start_time,
        "end_time": end_time,
        # Left as None when blank so the model can derive it from start/end.
        "hours": _decimal_or_none(request.POST.get("hours")),
        "is_billable": bool(request.POST.get("is_billable")),
    }
    if values["hours"] is None and not (start_time and end_time):
        return None, "Enter hours, or a start and end time to derive them."
    return values, None


def _parse_time(raw):
    """('HH:MM' | '') -> (time|None, ok). Blank is valid and means None."""
    raw = (raw or "").strip()
    if not raw:
        return None, True
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).time(), True
        except ValueError:
            continue
    return None, False


def _resolve_logged_task(request, project):
    """The task a log is filed against, if any — must belong to the project."""
    task_pk = request.POST.get("task")
    if not task_pk:
        return None
    return visible_tasks(Task.objects.filter(pk=task_pk, project=project),
                         request.user).first()


@login_required
@require_POST
def worklog_create(request, pk=None):
    """Create an entry.

    Two entry points share this: the project page (project fixed in the URL)
    and the timesheet (project chosen in the form). The timesheet form posts a
    plain field rather than rewriting its action in JS, so it works without it.
    """
    if pk is None:
        chosen = (request.POST.get("project") or "").strip()
        if not chosen.isdigit():
            messages.error(request, "Choose a project to log against.")
            return redirect("worklogs:mine")
        project = get_object_or_404(
            visible_projects(Project.objects.all(), request.user), pk=int(chosen))
    else:
        project = get_object_or_404(visible_projects(Project.objects.all(), request.user), pk=pk)

    if not _worklog_module_access(request.user):
        messages.error(request, "You don't have access to work logs.")
        return redirect(project)

    values, error = _worklog_fields_from_post(request)
    if error:
        messages.error(request, error)
        return redirect(request.POST.get("next") or project.get_absolute_url())

    try:
        screenshot = process_upload(request.FILES.get("screenshot"))
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect(request.POST.get("next") or project.get_absolute_url())

    entry = WorkLogEntry(
        project=project,
        task=_resolve_logged_task(request, project),
        logged_by=request.user,
        screenshot=screenshot,
        **values,
    )
    entry.save()

    log_activity(request.user, "logged work", project.name)
    messages.success(request, f"Logged {entry.hours or 0} h.")
    return redirect(request.POST.get("next") or project.get_absolute_url())


@login_required
def worklog_edit(request, entry_pk):
    """Edit an entry. Editing an approved one sends it back for approval —
    the number changed, so the sign-off no longer covers it."""
    entry = get_object_or_404(
        scope(WorkLogEntry.objects.select_related(
                  "project", "project__client", "task", "logged_by"),
              request.user, path="project__workspace"),
        pk=entry_pk,
    )
    if not _can_edit_log(request.user, entry):
        messages.error(request, "You can only edit your own work logs.")
        return redirect(entry.project)

    if request.method == "POST":
        values, error = _worklog_fields_from_post(request)
        if error:
            messages.error(request, error)
            return redirect(request.path)

        was_approved = entry.is_locked
        # values["hours"] is None when the field was left blank, which clears
        # the old figure and lets save() re-derive it from the new start/end.
        for field, value in values.items():
            setattr(entry, field, value)
        entry.task = _resolve_logged_task(request, entry.project)
        if request.FILES.get("screenshot"):
            try:
                entry.screenshot = process_upload(request.FILES["screenshot"])
            except ValidationError as exc:
                messages.error(request, exc.messages[0])
                return redirect(request.path)

        if was_approved:
            entry.approval_status = WorkLogEntry.Approval.SUBMITTED
            entry.approved_by = None
            entry.approved_on = None
            messages.info(request,
                          "Edited an approved entry — it's back with the approver.")
        entry.save()

        log_activity(request.user, "updated work log", entry.project.name)
        messages.success(request, "Work log updated.")
        return redirect(request.POST.get("next") or entry.project.get_absolute_url())

    return render(request, "projects/worklog_form.html", {
        "entry": entry,
        "project": entry.project,
        "categories": WorkLogEntry.Category.choices,
        "tasks": visible_tasks(Task.objects.filter(project=entry.project),
                              request.user).order_by("title"),
        "next": request.GET.get("next", ""),
    })


@login_required
@require_POST
def worklog_delete(request, entry_pk):
    entry = get_object_or_404(
        scope(WorkLogEntry.objects.select_related("project"), request.user,
              path="project__workspace"), pk=entry_pk)
    if not _can_delete_log(request.user, entry):
        messages.error(request, "You can only delete your own work logs.")
        return redirect(entry.project)
    if entry.is_locked and not has_perm(request.user, "worklogs", "approve"):
        messages.error(request, "Approved time can't be deleted — ask an approver.")
        return redirect(request.POST.get("next") or entry.project.get_absolute_url())

    redirect_to = request.POST.get("next") or entry.project.get_absolute_url()
    entry.delete()  # model delete() resyncs the task's actual_hours
    messages.success(request, "Work log entry deleted.")
    return redirect(redirect_to)


# --- approval workflow ---

@login_required
@require_POST
def worklog_submit(request, entry_pk):
    """Send one entry for approval."""
    entry = get_object_or_404(
        scope(WorkLogEntry.objects.select_related("project"), request.user,
              path="project__workspace"), pk=entry_pk)
    if not _owns(request.user, entry):
        messages.error(request, "You can only submit your own work logs.")
        return redirect(entry.project)

    redirect_to = request.POST.get("next") or reverse("worklogs:mine")
    if entry.is_locked:
        messages.info(request, "That entry is already approved.")
        return redirect(redirect_to)

    entry.approval_status = WorkLogEntry.Approval.SUBMITTED
    entry.save(update_fields=["approval_status", "updated_at"])
    _notify_approvers(request, [entry])
    messages.success(request, "Sent for approval.")
    return redirect(redirect_to)


@login_required
@require_POST
def worklog_submit_week(request):
    """Submit every draft/rejected entry in one week — the timesheet action."""
    week_start = _parse_date(request.POST.get("week_start"))
    if week_start is None:
        messages.error(request, "Pick a week to submit.")
        return redirect("worklogs:mine")

    week_end = week_start + timedelta(days=6)
    pending = list(
        WorkLogEntry.objects
        .filter(logged_by=request.user, date__gte=week_start, date__lte=week_end,
                approval_status__in=[WorkLogEntry.Approval.NONE,
                                     WorkLogEntry.Approval.REJECTED])
        .select_related("project")
    )
    if not pending:
        messages.info(request, "Nothing to submit for that week.")
        return redirect("worklogs:mine")

    WorkLogEntry.objects.filter(pk__in=[e.pk for e in pending]).update(
        approval_status=WorkLogEntry.Approval.SUBMITTED)
    _notify_approvers(request, pending, week_start=week_start)

    log_activity(request.user, "submitted timesheet",
                 f"week of {week_start:%d %b %Y}")
    messages.success(
        request,
        f"Submitted {len(pending)} entr{'y' if len(pending) == 1 else 'ies'} "
        f"for the week of {week_start:%d %b}.")
    return redirect("worklogs:mine")


def _notify_approvers(request, entries, week_start=None):
    """Ping the logger's manager, falling back to worklogs.approve holders."""
    manager = request.user.reports_to
    if manager is not None:
        recipients = [manager]
    else:
        # One query. The old loop pulled every active user and ran has_perm per
        # user, which meant a permission query each.
        # Approvers from the entries' own workspace. Falling back to the
        # submitter's rather than to None matters: None means *unscoped* to
        # `users_with_perm`, which would put a partner's timesheet in front of
        # our managers.
        from core.tenancy import workspace_for_new
        entry_workspace = (entries[0].project.workspace_id if entries
                           else workspace_for_new(request.user).pk)
        recipients = list(users_with_perm("worklogs", "approve",
                                          workspace=entry_workspace))
    if not recipients:
        return

    who = request.user.get_full_name() or request.user.username
    if week_start:
        text = (f"{who} submitted {len(entries)} work log"
                f"{'' if len(entries) == 1 else 's'} "
                f"for the week of {week_start:%d %b}")
    else:
        text = f"{who} submitted a work log for approval"
    notify(recipients, text[:220], url=reverse("worklogs:approvals"),
           exclude=request.user)


@login_required
@require_POST
def worklog_approve(request, entry_pk):
    entry = get_object_or_404(
        scope(WorkLogEntry.objects.select_related("project", "logged_by"),
              request.user, path="project__workspace"), pk=entry_pk)
    if not _can_approve_log(request.user, entry):
        messages.error(request, "You can't approve this work log.")
        return redirect("worklogs:approvals")

    entry.approval_status = WorkLogEntry.Approval.APPROVED
    entry.approved_by = request.user
    entry.approved_on = timezone.now()
    entry.review_note = request.POST.get("note", "").strip()
    entry.save(update_fields=["approval_status", "approved_by", "approved_on",
                              "review_note", "updated_at"])

    log_activity(request.user, "approved work log", entry.project.name)
    if entry.logged_by:
        notify([entry.logged_by],
               f"Work log approved: {entry.date:%d %b} · {entry.project.name}",
               url=reverse("worklogs:mine"), exclude=request.user)
    messages.success(request, "Work log approved.")
    return redirect(request.POST.get("next") or reverse("worklogs:approvals"))


@login_required
@require_POST
def worklog_reject(request, entry_pk):
    """Rejection needs a note — otherwise the logger can't act on it."""
    entry = get_object_or_404(
        scope(WorkLogEntry.objects.select_related("project", "logged_by"),
              request.user, path="project__workspace"), pk=entry_pk)
    if not _can_approve_log(request.user, entry):
        messages.error(request, "You can't review this work log.")
        return redirect("worklogs:approvals")

    note = request.POST.get("note", "").strip()
    redirect_to = request.POST.get("next") or reverse("worklogs:approvals")
    if not note:
        messages.error(request, "Add a note explaining the rejection.")
        return redirect(redirect_to)

    entry.approval_status = WorkLogEntry.Approval.REJECTED
    entry.approved_by = None
    entry.approved_on = None
    entry.review_note = note
    entry.save(update_fields=["approval_status", "approved_by", "approved_on",
                              "review_note", "updated_at"])

    log_activity(request.user, "rejected work log", entry.project.name)
    if entry.logged_by:
        notify([entry.logged_by],
               f"Work log needs changes: {entry.date:%d %b} — {note[:80]}",
               url=reverse("worklogs:mine"), exclude=request.user)
    messages.success(request, "Sent back to the logger.")
    return redirect(redirect_to)


# --- timesheet pages ---

def _parse_date(raw):
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _week_start(day):
    """Monday of the week containing `day`."""
    return day - timedelta(days=day.weekday())


def _build_weeks(entries):
    """Group entries into weeks → days, with totals at both levels.

    Days are emitted Monday-to-Sunday whether or not they hold entries, so the
    timesheet reads as a calendar rather than a sparse list.
    """
    buckets = {}
    for entry in entries:
        buckets.setdefault(_week_start(entry.date), []).append(entry)

    weeks = []
    for start in sorted(buckets, reverse=True):
        rows = buckets[start]
        by_day = {}
        for entry in rows:
            by_day.setdefault(entry.date, []).append(entry)

        days = []
        for offset in range(7):
            day = start + timedelta(days=offset)
            day_entries = by_day.get(day, [])
            days.append({
                "date": day,
                "entries": day_entries,
                "total": sum((e.hours or Decimal("0")) for e in day_entries),
            })

        total = sum((e.hours or Decimal("0")) for e in rows)
        billable = sum((e.hours or Decimal("0")) for e in rows if e.is_billable)
        weeks.append({
            "start": start,
            "end": start + timedelta(days=6),
            "days": days,
            "entries": rows,
            "total": total,
            "billable": billable,
            "non_billable": total - billable,
            "billable_percent": round(billable * 100 / total) if total else 0,
            "submittable": sum(
                1 for e in rows
                if e.approval_status in (WorkLogEntry.Approval.NONE,
                                         WorkLogEntry.Approval.REJECTED)),
            "awaiting": sum(1 for e in rows if e.is_awaiting_approval),
        })
    return weeks


@login_required
def my_worklogs(request):
    """Personal timesheet: entries by week, with totals and a quick add form."""
    if not _worklog_module_access(request.user):
        messages.error(request, "You don't have 'view' permission on work logs.")
        return redirect("core:dashboard")

    today = timezone.localdate()
    # A rolling window keeps the page bounded; older time lives in reports.
    since = _week_start(today) - timedelta(weeks=7)
    entries = list(
        WorkLogEntry.objects
        .filter(logged_by=request.user, date__gte=since)
        .select_related("project", "project__client", "task", "approved_by")
        .order_by("-date", "-created_at")
    )

    is_approver = _is_any_approver(request.user)
    return render(request, "projects/my_worklogs.html", {
        "weeks": _build_weeks(entries),
        "month_summary": WorkLogEntry.user_month_summary(
            request.user, today.year, today.month),
        "categories": WorkLogEntry.Category.choices,
        "projects": Project.objects.filter(is_archived=False)
                    .select_related("client").order_by("name"),
        "recent_tasks": Task.objects.filter(assignee=request.user)
                        .exclude(status=Task.Status.DONE)
                        .select_related("project").order_by("project__name", "title"),
        "is_approver": is_approver,
        # Only counted for approvers — otherwise it's a wasted query per load.
        "pending_count": (_pending_for_approver(request.user).count()
                          if is_approver else 0),
        "today": today,
        "this_week": _week_start(today),
    })


def _pending_for_approver(user):
    """SUBMITTED entries this user may act on."""
    qs = (WorkLogEntry.objects
          .filter(approval_status=WorkLogEntry.Approval.SUBMITTED)
          .select_related("project", "project__client", "task", "logged_by"))
    if has_perm(user, "worklogs", "approve"):
        return qs
    return qs.filter(logged_by__reports_to=user)


@login_required
def worklog_approvals(request):
    """Queue of work logs waiting on this user."""
    if not _is_any_approver(request.user):
        messages.error(request, "You don't have approval rights on work logs.")
        return redirect("worklogs:mine")

    pending = list(_pending_for_approver(request.user).order_by("logged_by", "date"))

    # Group by person: approvers review a timesheet, not a flat list of rows.
    by_person = {}
    for entry in pending:
        by_person.setdefault(entry.logged_by, []).append(entry)
    groups = [
        {"person": person, "entries": rows,
         "total": sum((e.hours or Decimal("0")) for e in rows)}
        for person, rows in by_person.items()
    ]

    return render(request, "projects/worklog_approvals.html", {
        "groups": groups,
        "pending_count": len(pending),
        "today": timezone.localdate(),
    })


# --- assets ---

@login_required
@require_perm("assets", "create")
@require_POST
def asset_upload(request, pk):
    project = get_object_or_404(visible_projects(Project.objects.all(), request.user), pk=pk)
    upload = request.FILES.get("file")
    link = request.POST.get("external_url", "").strip()
    category = request.POST.get("category", Asset.Category.OTHER)
    title = request.POST.get("title", "").strip()

    if link and not upload:
        try:
            link = normalise_drive_link(link)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect(project)
    elif upload:
        try:
            upload = process_upload(upload)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect(project)
        link = ""
    else:
        messages.error(request, empty_upload_message())
        return redirect(project)

    asset = Asset.objects.create(
        project=project,
        title=title or (upload.name if upload else "Linked file"),
        file=upload or "",  # not None — the column is NOT NULL, empty means "linked"
        external_url=link,
        category=(category if category in Asset.Category.values
                  else Asset.Category.OTHER),
        is_report_proof=bool(request.POST.get("is_report_proof")),
        notes=request.POST.get("notes", ""),
        uploaded_by=request.user,
    )
    log_activity(request.user, "uploaded asset", f"{asset.title} · {project.name}")
    return redirect(project)


@login_required
@require_perm("assets", "delete")
@require_POST
def asset_delete(request, asset_pk):
    asset = get_object_or_404(
        scope(Asset.objects.select_related("project"), request.user,
              path="project__workspace"), pk=asset_pk)
    project = asset.project
    asset.file.delete(save=False)
    asset.delete()
    messages.success(request, "Asset deleted.")
    return redirect(project)


# --- milestones ---

@login_required
@require_perm("milestones", "create")
@require_POST
def milestone_create(request, pk):
    project = get_object_or_404(visible_projects(Project.objects.all(), request.user), pk=pk)
    title = request.POST.get("title", "").strip()
    if not title:
        messages.error(request, "Milestone title is required.")
        return redirect(project)
    order = project.milestones.count()
    Milestone.objects.create(
        project=project, title=title,
        due_date=_parse_date(request.POST.get("due_date")),
        description=request.POST.get("description", ""),
        order=order,
    )
    log_activity(request.user, "added milestone", f"{title} · {project.name}")
    return redirect(project)


@login_required
@require_perm("milestones", "edit")
def milestone_edit(request, milestone_pk):
    milestone = get_object_or_404(
        scope(Milestone.objects.select_related("project", "project__client"),
              request.user, path="project__workspace"),
        pk=milestone_pk,
    )
    if request.method == "POST":
        milestone.title = (request.POST.get("title", milestone.title).strip()
                           or milestone.title)
        milestone.description = request.POST.get("description", "")
        milestone.due_date = _parse_date(request.POST.get("due_date"))
        status = request.POST.get("status", "")
        if status in Milestone.Status.values:
            milestone.apply_status(status)
        milestone.save()
        messages.success(request, "Milestone updated.")
        return redirect(milestone.project)
    return render(request, "projects/milestone_form.html", {
        "milestone": milestone,
        "project": milestone.project,
        "statuses": Milestone.Status.choices,
    })


@login_required
@require_perm("milestones", "edit")
@require_POST
def milestone_set_status(request, milestone_pk):
    milestone = get_object_or_404(
        scope(Milestone.objects.select_related("project"), request.user,
              path="project__workspace"),
                                  pk=milestone_pk)
    status = request.POST.get("status", "")
    if status in Milestone.Status.values:
        milestone.save(update_fields=milestone.apply_status(status))
        if status == Milestone.Status.DONE:
            log_activity(request.user, "completed milestone",
                         f"{milestone.title} · {milestone.project.name}")
    return redirect(milestone.project)


@login_required
@require_perm("milestones", "delete")
@require_POST
def milestone_delete(request, milestone_pk):
    milestone = get_object_or_404(
        scope(Milestone.objects.select_related("project"), request.user,
              path="project__workspace"),
                                  pk=milestone_pk)
    project = milestone.project
    milestone.delete()
    messages.success(request, "Milestone deleted.")
    return redirect(project)


# --- onboarding ---

@login_required
@require_perm("projects", "approve")
@require_POST
def onboarding_toggle(request, item_pk):
    item = get_object_or_404(
        scope(OnboardingItem.objects.select_related("project"), request.user,
              path="project__workspace"), pk=item_pk
    )
    if item.is_auto:
        messages.error(request, f"“{item.label}” is tracked automatically from payments.")
        return redirect(item.project)
    item.is_done = not item.is_done
    if item.is_done:
        item.completed_on = timezone.localdate()
        item.completed_by = request.user
        verb = "completed onboarding step"
    else:
        item.completed_on = None
        item.completed_by = None
        verb = "reopened onboarding step"
    item.save(update_fields=["is_done", "completed_on", "completed_by", "updated_at"])
    log_activity(request.user, verb, f"{item.label} · {item.project.name}")
    return redirect(item.project)
