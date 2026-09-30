from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.permissions import require_perm
from core.models import log_activity
from core.tenancy import workspace_for_new
from documents.access import visible_documents
from documents.models import Document
from projects.access import can_view_all_projects, visible_projects
from projects.models import Project
from client_calendar.models import ClientCalendarLink
from .access import visible_clients
from .models import Client, Contact


@login_required
@require_perm("clients", "view")
def client_list(request):
    # A client is listed only when the viewer can see at least one of its
    # projects; the card's project count reads from the same narrowed set.
    project_qs = visible_projects(
        Project.objects.filter(is_archived=False), request.user)
    # Workspace first, membership second. A client with no projects yet still
    # belongs to somebody, so the partition filter has to be on the client
    # queryset itself rather than inherited from the project prefetch.
    clients = (visible_clients(
                   Client.objects.filter(is_archived=False), request.user)
               .prefetch_related(Prefetch("projects", queryset=project_qs,
                                          to_attr="visible_projects")))
    if not can_view_all_projects(request.user):
        clients = clients.filter(
            projects__pk__in=project_qs.values("pk")).distinct()
    q = request.GET.get("q", "").strip()
    if q:
        clients = clients.filter(name__icontains=q)
    return render(request, "clients/list.html", {"clients": clients, "q": q})


@login_required
@require_perm("clients", "create")
def client_create(request):
    if request.method == "POST":
        client = Client.objects.create(
            name=request.POST["name"],
            email=request.POST.get("email", ""),
            phone=request.POST.get("phone", ""),
            website=request.POST.get("website", ""),
            gstin=request.POST.get("gstin", ""),
            city=request.POST.get("city", ""),
            state=request.POST.get("state", ""),
            billing_address=request.POST.get("billing_address", ""),
            notes=request.POST.get("notes", ""),
            created_by=request.user,
            workspace=workspace_for_new(request.user),
        )
        contact_name = request.POST.get("contact_name", "").strip()
        if contact_name:
            Contact.objects.create(
                client=client, name=contact_name,
                email=request.POST.get("contact_email", ""),
                phone=request.POST.get("contact_phone", ""),
                designation=request.POST.get("contact_designation", ""),
                is_primary=True,
            )
        log_activity(request.user, "created client", client.name)
        return redirect(client)
    return render(request, "clients/form.html")


@login_required
@require_perm("clients", "edit")
def client_edit(request, pk):
    client = get_object_or_404(
        visible_clients(Client.objects.all(), request.user), pk=pk)
    contact = client.primary_contact
    if request.method == "POST":
        client.name = request.POST.get("name", client.name).strip() or client.name
        for field in ("email", "phone", "website", "gstin", "city", "state",
                      "billing_address", "notes"):
            setattr(client, field, request.POST.get(field, ""))
        client.save()
        contact_name = request.POST.get("contact_name", "").strip()
        if contact:
            if contact_name:
                contact.name = contact_name
                contact.email = request.POST.get("contact_email", "")
                contact.phone = request.POST.get("contact_phone", "")
                contact.designation = request.POST.get("contact_designation", "")
                contact.save()
        elif contact_name:
            Contact.objects.create(
                client=client, name=contact_name,
                email=request.POST.get("contact_email", ""),
                phone=request.POST.get("contact_phone", ""),
                designation=request.POST.get("contact_designation", ""),
                is_primary=True,
            )
        log_activity(request.user, "updated client", client.name)
        messages.success(request, "Client updated.")
        return redirect(client)
    return render(request, "clients/form.html", {"client": client, "contact": contact})


@login_required
@require_perm("clients", "delete")
@require_POST
def client_delete(request, pk):
    client = get_object_or_404(
        visible_clients(Client.objects.all(), request.user), pk=pk)
    name = client.name
    client.delete()
    log_activity(request.user, "deleted client", name)
    messages.success(request,
                     f"“{name}” deleted, along with its projects and documents.")
    return redirect("clients:list")


@login_required
@require_perm("clients", "delete")
@require_POST
def client_bulk_delete(request):
    clients = list(visible_clients(Client.objects.all(), request.user)
                   .filter(pk__in=request.POST.getlist("selected")))
    if not clients:
        messages.error(request, "No clients selected.")
        return redirect("clients:list")
    for c in clients:
        log_activity(request.user, "deleted client", c.name)
    Client.objects.filter(pk__in=[c.pk for c in clients]).delete()
    messages.success(
        request,
        f"Deleted {len(clients)} client{'s' if len(clients) != 1 else ''}, "
        "along with their projects and documents.",
    )
    return redirect("clients:list")


@login_required
@require_perm("clients", "view")
def client_detail(request, pk):
    client = get_object_or_404(
        visible_clients(Client.objects.all(), request.user), pk=pk)
    document_qs = visible_documents(
        Document.objects.filter(is_archived=False)
        .select_related("document_type", "current_version")
        .order_by("-created_at"),
        request.user,
    )
    projects = visible_projects(
        client.projects.filter(is_archived=False), request.user
    ).prefetch_related(
        Prefetch("documents", queryset=document_qs, to_attr="visible_documents"),
        "onboarding_items",
    )
    return render(request, "clients/detail.html", {
        "client": client,
        "projects": projects,
        "contacts": client.contacts.all(),
        "calendar_link": ClientCalendarLink.objects.filter(client=client).first(),
    })
