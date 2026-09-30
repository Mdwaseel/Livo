"""Workspace management + the topbar switcher.

Two screens and one endpoint, all of them home-super-admin only:

* `workspace_list`   — the partitions on this install, and who is in each.
* `workspace_form`   — create/rename a partner workspace.
* `workspace_switch` — move the switcher, which is the only thing that ever
  widens a home admin's view beyond our own numbers.

The home workspace is deliberately not deletable and not renameable-away: it is
what every unstamped row falls back to, so losing it would strand our own data.
Partner workspaces are deactivated rather than deleted — their clients and
projects are real records, and a DELETE cascade here would take them with it.
"""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import Workspace, log_activity
from .tenancy import ALL, SESSION_KEY, home_admin_required

User = get_user_model()


@login_required
@home_admin_required
def workspace_list(request):
    workspaces = (Workspace.objects
                  .annotate(
                      people=Count("users", filter=Q(users__is_active=True),
                                   distinct=True),
                      client_count=Count("clients",
                                         filter=Q(clients__is_archived=False),
                                         distinct=True),
                      project_count=Count("projects",
                                          filter=Q(projects__is_archived=False),
                                          distinct=True))
                  .order_by("-is_home", "name"))
    people = (User.objects.filter(is_active=True)
              .select_related("workspace", "primary_role")
              .order_by("first_name", "username"))
    return render(request, "core/workspaces.html",
                  {"workspaces": workspaces, "people": people})


@login_required
@home_admin_required
def workspace_form(request, pk=None):
    workspace = get_object_or_404(Workspace, pk=pk) if pk else None
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "A workspace needs a name.")
            return render(request, "core/workspace_form.html",
                          {"workspace": workspace, "posted": request.POST})
        clash = Workspace.objects.filter(name__iexact=name)
        if workspace:
            clash = clash.exclude(pk=workspace.pk)
        if clash.exists():
            messages.error(request, f"A workspace called “{name}” already exists.")
            return render(request, "core/workspace_form.html",
                          {"workspace": workspace, "posted": request.POST})

        if workspace is None:
            workspace = Workspace(name=name)
        else:
            workspace.name = name
        workspace.color = (request.POST.get("color") or "#6366F1")[:7]
        workspace.notes = request.POST.get("notes", "")
        # The home workspace can never be switched off — it is where every
        # unstamped row lives, and an inactive one would hide our own data
        # from us.
        workspace.is_active = workspace.is_home or bool(request.POST.get("is_active"))
        workspace.save()
        log_activity(request.user, "saved workspace", workspace.name)
        messages.success(request, f"Workspace “{workspace.name}” saved.")
        return redirect("core:workspace_list")
    return render(request, "core/workspace_form.html", {"workspace": workspace})


@login_required
@home_admin_required
@require_POST
def workspace_assign(request):
    """Save the whole people-to-workspace table in one pass.

    Only rows that actually changed are written, so the activity log records
    moves rather than a line per person every time somebody hits Save.
    """
    workspaces = {w.pk: w for w in Workspace.objects.all()}
    people = (User.objects.filter(is_active=True)
              .select_related("workspace").exclude(pk=request.user.pk))

    moved = []
    for person in people:
        raw = request.POST.get(f"ws_{person.pk}")
        if not raw or not raw.isdigit():
            continue
        target = workspaces.get(int(raw))
        if target is None or target.pk == person.workspace_id:
            continue
        previous = person.workspace.name if person.workspace else "—"
        person.workspace = target
        person.save(update_fields=["workspace"])
        log_activity(request.user, "moved user to workspace",
                     person.get_full_name() or person.username,
                     f"{previous} → {target.name}")
        moved.append(person)

    if moved:
        messages.success(
            request,
            f"Moved {len(moved)} "
            f"{'person' if len(moved) == 1 else 'people'}. "
            "They'll see the new workspace on their next page load.")
    else:
        messages.info(request, "No changes to save.")
    return redirect("core:workspace_list")


@login_required
@require_POST
def workspace_switch(request):
    """Move the topbar switcher.

    Not decorated with `home_admin_required`: a non-home user posting here
    should be told no in the ordinary way rather than shown a 403 page, and the
    check is the same one `can_switch_workspace` makes for the widget itself.
    """
    from .tenancy import can_switch_workspace

    if not can_switch_workspace(request.user):
        messages.error(request, "You can't change workspace.")
        return redirect("core:dashboard")

    choice = (request.POST.get("workspace") or "").strip()
    if choice == ALL:
        request.session[SESSION_KEY] = ALL
    elif choice.isdigit() and Workspace.objects.filter(
            pk=choice, is_active=True).exists():
        request.session[SESSION_KEY] = int(choice)
    else:
        # Anything unrecognised puts the switcher back home rather than leaving
        # it wherever it was — an unreadable value must not silently keep a
        # partner's workspace active.
        request.session.pop(SESSION_KEY, None)
    return redirect(request.META.get("HTTP_REFERER") or "core:dashboard")
