"""Employee directory + profiles.

Visibility layers (all through the RBAC engine):
  - directory + basic profile (overview/contact/skills): employees.view
  - documents + sensitive block (salary, bank):           payroll.view
    …except everyone may see THEIR OWN documents.
  - edit basic fields (address/phone/emergency/personal email/skills/photo):
    employees.edit OR own profile
  - edit HR fields (salary/bank/status/joining/exit/role/org): employees.edit
    AND payroll.view
Anyone can open their OWN profile regardless of role ("My profile").
Controls the viewer lacks are hidden in templates, and re-checked server-side.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Department, Designation, Role
from accounts.permissions import (SUPER_ADMIN, has_perm, is_superadmin,
                                  require_perm)
from core.models import log_activity
from core.tenancy import is_home_user, workspace_for_new
from core.uploads import empty_upload_message, normalise_drive_link, process_upload
from . import leave
from .models import EmployeeDocument, EmployeeProfile, Skill

User = get_user_model()


# ---------- permission helpers ----------

def _is_self(user, profile):
    return profile.user_id == user.pk

def _can_view_basic(user, profile):
    return has_perm(user, "employees", "view") or _is_self(user, profile)

def _shares_workspace(user, profile):
    """Same partition — the test that decides whose payroll is whose."""
    return getattr(user, "workspace_id", None) == profile.user.workspace_id


def _can_view_sensitive(user, profile=None):
    """Salary, bank details and HR documents.

    Two gates, and the second is the partner rule rather than an RBAC one. A
    partner is a super admin of *their own world*, and `has_perm` short-circuits
    to True for every super admin — so `payroll.view` alone would hand anyone
    running a partner workspace our salary column. The employee DIRECTORY is
    shared across workspaces by design; what people are paid is not.

    So: home staff see payroll as before, anywhere. Everyone else sees it only
    for people in their own workspace. `profile=None` means the caller is asking
    the blanket question ("can this person work with payroll at all"), which
    only home staff can answer yes to.
    """
    if not has_perm(user, "payroll", "view"):
        return False
    if is_home_user(user):
        return True
    return profile is not None and _shares_workspace(user, profile)

def _can_view_docs(user, profile):
    return _can_view_sensitive(user, profile) or _is_self(user, profile)

def _can_edit_basic(user, profile):
    return has_perm(user, "employees", "edit") or _is_self(user, profile)

def _can_edit_hr(user, profile=None):
    return (has_perm(user, "employees", "edit")
            and _can_view_sensitive(user, profile))


def _can_assign_roles(user):
    """Granting RBAC roles is a super-admin act, wherever the form lives.

    This screen is reachable with employees.edit + payroll.view — which HR
    holds — so without this gate HR could hand out Super Admin (including to
    themselves) from the HR tab. Role assignment lives at
    accounts.views.user_assign, which is @superadmin_required; this mirrors it.
    """
    return is_superadmin(user)


def _parse_date(raw):
    """('YYYY-MM-DD' | '' | junk) -> date | None. Never raises: these values
    used to be handed to the DB layer raw, so a malformed date 500'd."""
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _decimal_or_none(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def _set_skills(profile, raw):
    """Comma-separated names -> Skill rows (created on demand)."""
    names = {n.strip() for n in raw.split(",") if n.strip()}
    skills = [Skill.objects.get_or_create(name__iexact=n,
                                          defaults={"name": n})[0] for n in names]
    profile.skills.set(skills)


# ---------- directory ----------

@login_required
@require_perm("employees", "view")
def directory(request):
    profiles = (EmployeeProfile.objects
                .select_related("user", "user__department", "user__designation")
                .prefetch_related("skills"))
    q = request.GET.get("q", "").strip()
    if q:
        profiles = profiles.filter(
            Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q)
            | Q(user__username__icontains=q) | Q(skills__name__icontains=q)
        ).distinct()
    department = request.GET.get("department", "")
    if department.isdigit():
        profiles = profiles.filter(user__department_id=department)
    designation = request.GET.get("designation", "")
    if designation.isdigit():
        profiles = profiles.filter(user__designation_id=designation)
    status = request.GET.get("status", "")
    if status in EmployeeProfile.Status.values:
        profiles = profiles.filter(status=status)
    return render(request, "employees/directory.html", {
        "profiles": profiles,
        "departments": Department.objects.all(),
        "designations": Designation.objects.select_related("department"),
        "statuses": EmployeeProfile.Status.choices,
        "q": q, "department": department, "designation": designation,
        "status": status,
    })


# ---------- detail ----------

@login_required
def detail(request, pk):
    profile = get_object_or_404(
        EmployeeProfile.objects.select_related(
            "user", "user__department", "user__designation", "user__reports_to",
            "user__primary_role"),
        pk=pk,
    )
    if not _can_view_basic(request.user, profile):
        messages.error(request, "You don't have permission to view employee profiles.")
        return redirect("core:dashboard")
    show_docs = _can_view_docs(request.user, profile)
    can_offboard = _can_offboard(request.user, profile)
    # Only a Super Admin is offered the permanent delete, and only they pay for
    # the three COUNT queries behind its warning.
    can_delete_employee = is_superadmin(request.user)
    # Payroll (salary/bank) shows to payroll viewers AND to the person
    # themselves — everyone can see their own compensation on their account.
    show_sensitive = (_can_view_sensitive(request.user, profile)
                      or _is_self(request.user, profile))
    return render(request, "employees/detail.html", {
        "profile": profile,
        "person": profile.user,
        "is_self": _is_self(request.user, profile),
        "show_sensitive": show_sensitive,
        "show_docs": show_docs,
        "documents": profile.documents.select_related("uploaded_by") if show_docs else [],
        "can_edit_basic": _can_edit_basic(request.user, profile),
        "can_edit_hr": _can_edit_hr(request.user, profile),
        # Adding documents to OTHER people's profiles is Manager / Super Admin
        # only (employees.create); everyone may still upload to their own.
        "can_upload": (has_perm(request.user, "employees", "create")
                       or _is_self(request.user, profile)),
        "can_delete_doc": has_perm(request.user, "employees", "delete"),
        "can_offboard": can_offboard,
        "can_delete_employee": can_delete_employee,
        "removal_blocker": (_removal_blocker(request.user, profile)
                            if can_offboard or can_delete_employee else None),
        "removal_impact": (_removal_impact(profile) if can_delete_employee else None),
        "today": timezone.localdate(),
        "exit_statuses": [
            (EmployeeProfile.Status.RESIGNED, "Resigned"),
            (EmployeeProfile.Status.TERMINATED, "Terminated"),
        ],
        "doc_types": EmployeeDocument.DocType.choices,
        # Only computed for viewers cleared for the compensation block it sits
        # in — a leave balance next to a salary is payroll information.
        "leave_balance": leave.balance(profile) if show_sensitive else None,
        "reports": (EmployeeProfile.objects
                    .filter(user__reports_to=profile.user)
                    .select_related("user", "user__designation")),
    })


@login_required
def my_profile(request):
    """Self-service entry point — works for every logged-in user.

    get_or_create rather than a bare attribute read: the auto-create signal
    only fires for users created after it shipped, so anyone predating it (or
    loaded from a fixture, which doesn't emit post_save with created=True)
    would have hit RelatedObjectDoesNotExist here.
    """
    profile, _ = EmployeeProfile.objects.get_or_create(user=request.user)
    return redirect(profile)


# ---------- create employee ----------

@login_required
@require_perm("employees", "create")
def create(request):
    ctx = {
        "roles": Role.objects.all(),
        "departments": Department.objects.all(),
        "designations": Designation.objects.select_related("department"),
        "managers": User.objects.filter(is_active=True)
                                .order_by("first_name", "username"),
        "statuses": EmployeeProfile.Status.choices,
    }
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        if not username or not password:
            messages.error(request, "Username and a temporary password are required.")
            return render(request, "employees/create.html", ctx)
        if User.objects.filter(username__iexact=username).exists():
            messages.error(request, f"Username “{username}” is already taken.")
            return render(request, "employees/create.html", ctx)
        with transaction.atomic():
            person = User.objects.create_user(
                username=username, password=password,
                email=request.POST.get("email", "").strip(),
                first_name=request.POST.get("first_name", "").strip(),
                last_name=request.POST.get("last_name", "").strip(),
            )
            person.phone = request.POST.get("phone", "").strip()
            person.primary_role_id = request.POST.get("primary_role") or None
            person.department_id = request.POST.get("department") or None
            person.designation_id = request.POST.get("designation") or None
            person.reports_to_id = request.POST.get("reports_to") or None
            person.employee_code = request.POST.get("employee_code", "").strip()
            # New people join the partition of whoever hired them, which is
            # what decides the data THEY will see. It has no bearing on who can
            # see them: the directory is shared across workspaces.
            person.workspace = workspace_for_new(request.user)
            person.save()
            profile = person.employee_profile  # created by signal
            profile.date_of_joining = _parse_date(request.POST.get("date_of_joining"))
            status = request.POST.get("status", "")
            if status in EmployeeProfile.Status.values:
                profile.status = status
            profile.save()
        log_activity(request.user, "added employee", person.get_full_name() or username)
        messages.success(request,
                         f"Employee “{person.get_full_name() or username}” created. "
                         "Share the temporary password with them securely.")
        return redirect(profile)
    return render(request, "employees/create.html", ctx)


# ---------- edit: basic (self or employees.edit) ----------

@login_required
def edit_basic(request, pk):
    profile = get_object_or_404(EmployeeProfile.objects.select_related("user"), pk=pk)
    if not _can_edit_basic(request.user, profile):
        messages.error(request, "You can't edit this profile.")
        return redirect(profile)
    person = profile.user
    if request.method == "POST":
        person.phone = request.POST.get("phone", person.phone).strip()
        person.save(update_fields=["phone"])
        # On the basic form rather than the HR one, so people can set their own
        # birthday without needing an HR permission to do it.
        profile.date_of_birth = _parse_date(request.POST.get("date_of_birth"))
        profile.personal_email = request.POST.get("personal_email", "")
        profile.address = request.POST.get("address", "")
        profile.emergency_contact_name = request.POST.get("emergency_contact_name", "")
        profile.emergency_contact_phone = request.POST.get("emergency_contact_phone", "")
        if request.FILES.get("photo"):
            try:
                profile.photo = process_upload(request.FILES["photo"])
            except ValidationError as exc:
                messages.error(request, exc.messages[0])
                return redirect(request.path)
        profile.save()
        _set_skills(profile, request.POST.get("skills", ""))
        log_activity(request.user, "updated profile", profile.display_name)
        messages.success(request, "Profile updated.")
        return redirect(profile)
    return render(request, "employees/edit_basic.html", {
        "profile": profile, "person": person,
        "skills_csv": ", ".join(profile.skills.values_list("name", flat=True)),
        "is_self": _is_self(request.user, profile),
    })


# ---------- edit: HR fields (employees.edit AND payroll.view) ----------

@login_required
def edit_hr(request, pk):
    # The profile is loaded first because the payroll gate is now per-person:
    # "may you edit HR fields" has no answer until we know whose.
    profile = get_object_or_404(
        EmployeeProfile.objects.select_related("user"), pk=pk)
    if not _can_edit_hr(request.user, profile):
        messages.error(request,
                       "HR fields need both employees.edit and payroll.view access.")
        return redirect("employees:detail", pk=pk)
    person = profile.user
    can_assign_roles = _can_assign_roles(request.user)
    if request.method == "POST":
        profile.date_of_joining = _parse_date(request.POST.get("date_of_joining"))
        profile.date_of_exit = _parse_date(request.POST.get("date_of_exit"))
        status = request.POST.get("status", "")
        if status in EmployeeProfile.Status.values:
            profile.status = status
        salary_raw = (request.POST.get("salary") or "").strip()
        salary = _decimal_or_none(salary_raw)
        if salary_raw and salary is None:
            messages.error(request,
                           f"Couldn't read “{salary_raw}” as an amount — salary "
                           "left unchanged.")
        else:
            profile.salary = salary
        allowance = (request.POST.get("annual_leave_days") or "").strip()
        # Blank means "use the agency default", which is a real setting rather
        # than a missing one — see employees.leave.allowance_for.
        profile.annual_leave_days = int(allowance) if allowance.isdigit() else None
        profile.bank_account_name = request.POST.get("bank_account_name", "")
        profile.bank_account_number = request.POST.get("bank_account_number", "")
        profile.bank_ifsc = request.POST.get("bank_ifsc", "")
        profile.save()
        if can_assign_roles:
            person.primary_role_id = request.POST.get("primary_role") or None
        elif ("primary_role" in request.POST
                and str(request.POST["primary_role"] or "")
                != str(person.primary_role_id or "")):
            # The select is hidden for non-super-admins, so a mismatch means a
            # hand-crafted POST.
            messages.error(request, "Only a Super Admin can change someone's role.")
        person.department_id = request.POST.get("department") or None
        person.designation_id = request.POST.get("designation") or None
        reports_to = request.POST.get("reports_to") or None
        person.reports_to_id = None if reports_to == str(person.pk) else reports_to
        person.employee_code = request.POST.get("employee_code", "").strip()
        person.save()
        log_activity(request.user, "updated HR record", profile.display_name)
        messages.success(request, "HR record updated.")
        return redirect(profile)
    return render(request, "employees/edit_hr.html", {
        "profile": profile, "person": person,
        "can_assign_roles": can_assign_roles,
        "roles": Role.objects.all(),
        "departments": Department.objects.all(),
        "designations": Designation.objects.select_related("department"),
        "managers": User.objects.filter(is_active=True).exclude(pk=person.pk)
                                .order_by("first_name", "username"),
        "statuses": EmployeeProfile.Status.choices,
    })


# ---------- documents ----------

@login_required
@require_POST
def document_upload(request, pk):
    profile = get_object_or_404(EmployeeProfile.objects.select_related("user"), pk=pk)
    if not (has_perm(request.user, "employees", "create")
            or _is_self(request.user, profile)):
        messages.error(request, "You can't upload documents for this employee.")
        return redirect(profile)
    upload = request.FILES.get("file")
    link = request.POST.get("external_url", "").strip()
    doc_type = request.POST.get("doc_type", EmployeeDocument.DocType.OTHER)

    if link and not upload:
        try:
            link = normalise_drive_link(link)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect(profile)
    elif upload:
        try:
            upload = process_upload(upload)
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
            return redirect(profile)
        link = ""
    else:
        messages.error(request, empty_upload_message())
        return redirect(profile)

    EmployeeDocument.objects.create(
        employee=profile, file=upload or "", external_url=link,
        doc_type=(doc_type if doc_type in EmployeeDocument.DocType.values
                  else EmployeeDocument.DocType.OTHER),
        notes=request.POST.get("notes", ""),
        uploaded_by=request.user,
    )
    log_activity(request.user, "uploaded employee document", profile.display_name)
    messages.success(request, "Document saved." if link else "Document uploaded.")
    return redirect(profile)


@login_required
def document_download(request, doc_pk):
    doc = get_object_or_404(
        EmployeeDocument.objects.select_related("employee", "employee__user"),
        pk=doc_pk,
    )
    if not _can_view_docs(request.user, doc.employee):
        messages.error(request, "You don't have permission to open that document.")
        return redirect("core:dashboard")
    if doc.is_link:
        # Nothing is stored here — the permission check above is still the gate
        # for reaching the link, but Drive enforces its own sharing rules too.
        return redirect(doc.external_url)
    if not doc.file:
        messages.error(request, "That document has no file attached.")
        return redirect(doc.employee)
    return FileResponse(doc.file.open("rb"), as_attachment=True,
                        filename=doc.filename)


@login_required
@require_POST
def document_delete(request, doc_pk):
    doc = get_object_or_404(EmployeeDocument.objects.select_related("employee"),
                            pk=doc_pk)
    profile = doc.employee
    if not has_perm(request.user, "employees", "delete"):
        messages.error(request,
                       "Only a Manager or Super Admin can delete employee documents.")
        return redirect(profile)
    doc.file.delete(save=False)
    doc.delete()
    messages.success(request, "Document deleted.")
    return redirect(profile)


# ---------- offboarding & removal ----------
#
# "Remove this employee" is two different acts and the screen says so, because
# picking the wrong one is not recoverable:
#
#   Offboard  — the account stops working, the record stays. What leaving a job
#               actually means, and what you want almost every time.
#   Delete    — the person and their payslips, leave and HR documents are gone.
#               Super Admin only, and typed confirmation, because nothing in
#               this app puts them back.
#
# Until now neither existed. Marking somebody RESIGNED on the HR tab set a
# label and nothing else: the account it described still signed in the next
# morning.

def _can_offboard(user, profile):
    """`employees.delete` — the same right that already guards employee
    documents — plus the partner rule that guards every other piece of HR data.
    The directory is deliberately shared across workspaces; the authority to
    close an account in one of them is not."""
    return (has_perm(user, "employees", "delete")
            and (is_home_user(user) or _shares_workspace(user, profile)))


def _is_last_super_admin(profile):
    """The one mistake here that the app cannot undo for you.

    Super Admin is granted at /settings/roles/, which is itself super-admin
    only. Close the last account holding it and there is nobody left who can
    grant it back — the way out is a shell on the server and `manage.py`. So
    both offboarding and deletion refuse, rather than asking for confirmation
    of something nobody can confirm their way out of.
    """
    person = profile.user
    if not is_superadmin(person):
        return False
    return not (User.objects
                .filter(is_active=True)
                .exclude(pk=person.pk)
                .filter(Q(is_superuser=True) | Q(primary_role__name=SUPER_ADMIN)
                        | Q(extra_roles__name=SUPER_ADMIN))
                .exists())


def _removal_blocker(actor, profile):
    """Why this person can't be removed, or None.

    Shared by the two destructive views and by the template that offers them,
    so the button you are shown is the button that will work — a confirmation
    dialog answered "yes" and then refused is a worse experience than never
    being offered it.
    """
    if _is_self(actor, profile):
        return "You can't remove your own account — ask another Super Admin to do it."
    if _is_last_super_admin(profile):
        return ("This is the last Super Admin who can still sign in. Give the role "
                "to somebody else first, or there will be nobody left who can.")
    return None


def _removal_impact(profile):
    """What a permanent delete destroys, counted for the confirmation panel.

    Everything else this person touched — tasks, work logs, documents they
    wrote, the activity log — is SET_NULL and survives with the author blanked.
    These three cascade, so they are the ones worth naming out loud.
    """
    from resource_planner.models import LeaveRecord
    return {
        "payslips": profile.salary_records.count(),
        "leave_records": LeaveRecord.objects.filter(user=profile.user).count(),
        "hr_documents": profile.documents.count(),
    }


@login_required
@require_POST
def offboard(request, pk):
    """Revoke access, keep the record."""
    profile = get_object_or_404(EmployeeProfile.objects.select_related("user"), pk=pk)
    if not _can_offboard(request.user, profile):
        messages.error(request, "Only a Manager or Super Admin can offboard someone.")
        return redirect(profile)
    blocker = _removal_blocker(request.user, profile)
    if blocker:
        messages.error(request, blocker)
        return redirect(profile)

    status = request.POST.get("status", "")
    if status not in {EmployeeProfile.Status.RESIGNED,
                      EmployeeProfile.Status.TERMINATED}:
        status = EmployeeProfile.Status.RESIGNED
    person = profile.user
    with transaction.atomic():
        profile.status = status
        profile.date_of_exit = (_parse_date(request.POST.get("date_of_exit"))
                                or timezone.localdate())
        profile.save(update_fields=["status", "date_of_exit", "updated_at"])
        # The line that makes this mean anything.
        person.is_active = False
        person.save(update_fields=["is_active"])
    log_activity(request.user, "offboarded employee", profile.display_name,
                 f"{profile.get_status_display()} on "
                 f"{profile.date_of_exit:%d %b %Y}")
    messages.success(
        request,
        f"{profile.display_name} can no longer sign in. Their payslips, leave "
        "and documents are untouched.")
    return redirect(profile)


@login_required
@require_POST
def reactivate(request, pk):
    """Undo an offboarding — someone came back, or it was the wrong person."""
    profile = get_object_or_404(EmployeeProfile.objects.select_related("user"), pk=pk)
    if not _can_offboard(request.user, profile):
        messages.error(request, "Only a Manager or Super Admin can restore access.")
        return redirect(profile)
    person = profile.user
    with transaction.atomic():
        profile.status = EmployeeProfile.Status.ACTIVE
        profile.date_of_exit = None
        profile.save(update_fields=["status", "date_of_exit", "updated_at"])
        person.is_active = True
        person.save(update_fields=["is_active"])
    log_activity(request.user, "restored employee access", profile.display_name)
    messages.success(request,
                     f"{profile.display_name} can sign in again. They will need "
                     "their existing password.")
    return redirect(profile)


@login_required
@require_POST
def delete(request, pk):
    """Erase the person and everything that cascades off them. Super Admin only.

    Not gated on `employees.delete` like offboarding is: that permission is
    grantable at /settings/roles/ and a Manager holds it, but destroying
    payroll history is not a thing to hand out through a checkbox matrix.
    """
    profile = get_object_or_404(EmployeeProfile.objects.select_related("user"), pk=pk)
    if not is_superadmin(request.user):
        messages.error(request,
                       "Only a Super Admin can permanently delete an employee. "
                       "Offboarding them keeps the record and stops their login.")
        return redirect(profile)
    blocker = _removal_blocker(request.user, profile)
    if blocker:
        messages.error(request, blocker)
        return redirect(profile)

    person = profile.user
    # Typed confirmation rather than a checkbox: this is reachable from a page
    # whose other buttons are all reversible, and a misclick here costs the
    # salary history of a real person.
    typed = (request.POST.get("confirm") or "").strip()
    if typed.lower() != person.username.lower():
        messages.error(request,
                       f"Type the username “{person.username}” exactly to confirm "
                       "deletion. Nothing was deleted.")
        return redirect(profile)

    name = profile.display_name
    impact = _removal_impact(profile)
    with transaction.atomic():
        # Files first, and inside the transaction: the rows are about to go, and
        # a deleted row with its upload still on disk is a private document
        # nobody can see to delete afterwards.
        if profile.photo:
            profile.photo.delete(save=False)
        for doc in profile.documents.all():
            if doc.file:
                doc.file.delete(save=False)
        # Deleting the User is what removes the profile, and through it the
        # payslips and documents — see EmployeeProfile.user (CASCADE).
        person.delete()
    log_activity(request.user, "deleted employee", name,
                 f"{impact['payslips']} payslip(s), {impact['leave_records']} leave "
                 f"record(s), {impact['hr_documents']} document(s) destroyed")
    messages.success(request, f"“{name}” and their HR history were deleted.")
    return redirect("employees:directory")
