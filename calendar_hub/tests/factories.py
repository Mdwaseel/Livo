"""Fixture for the calendar tests.

Pinned to a known month — July 2026 — rather than "today". Almost every
assertion here depends on which day of the week a date falls on, how many rows
a month grid needs, and whether a birthday has already passed; a fixture that
drifted with the real calendar would pass on a Tuesday and fail on a Saturday,
and the failure would look like a bug in the code.

July 2026 was chosen because it starts on a Wednesday and has 31 days, so its
month grid spills into both neighbours and needs five rows — the shape that
catches off-by-one errors in the grid builder.

One entry of every kind lands in the window, so a test that says "the calendar
shows nine kinds" is checking nine real records rather than a list of strings.
"""
from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model

from accounts.models import Department, Role
from calendar_hub.models import CalendarEvent
from client_calendar.models import ClientActivity
from clients.models import Client
from documents.models import Document, DocumentType
from employees.models import EmployeeProfile
from projects.models import Milestone, Project, Task
from resource_planner.models import LeaveRecord

User = get_user_model()

# A Wednesday, and the first of the month.
JULY = date(2026, 7, 1)
MONTH_START = JULY
MONTH_END = date(2026, 7, 31)
# The grid around July 2026: Mon 29 June to Sun 2 August. Five rows.
GRID_START = date(2026, 6, 29)
GRID_END = date(2026, 8, 2)

MONDAY = date(2026, 7, 6)
TUESDAY = MONDAY + timedelta(days=1)
WEDNESDAY = MONDAY + timedelta(days=2)
THURSDAY = MONDAY + timedelta(days=3)
FRIDAY = MONDAY + timedelta(days=4)
SATURDAY = MONDAY + timedelta(days=5)
SUNDAY = MONDAY + timedelta(days=6)


def make_user(username, *, role=None, department=None, born=None):
    user = User.objects.create_user(
        username=username, email=f"{username}@livodigital.com",
        password="calendar-test-pass-99", first_name=username.title())
    if role:
        user.primary_role = Role.objects.get(name=role)
    if department:
        user.department = department
    user.save()
    profile, _ = EmployeeProfile.objects.get_or_create(user=user)
    profile.status = EmployeeProfile.Status.ACTIVE
    if born:
        profile.date_of_birth = born
    profile.save()
    return user


class Scenario:
    """One of everything, in July 2026, with the dates chosen by hand.

    Mon 6 Jul   Anna's task is due; the weekly stand-up runs (recurring)
    Tue 7 Jul   the client review meeting, 14:00–15:00
    Wed 8 Jul   Anna's birthday (born 1992); the milestone is due
    Thu 9 Jul   the document review deadline
    Fri 10 Jul  the project deadline
    Mon 13–Wed 15   Bilal is on approved leave (three days)
    Wed 15 Jul  a company-wide holiday
    """

    def __init__(self):
        self.delivery = Department.objects.create(name="Delivery")
        self.design = Department.objects.create(name="Design")

        self.manager = make_user("mgr", role="Manager", department=self.delivery)
        self.pm = make_user("pm", role="Project Manager", department=self.delivery)
        self.anna = make_user("anna", role="Developer", department=self.delivery,
                              born=date(1992, 7, 8))
        self.bilal = make_user("bilal", role="Designer", department=self.design)
        self.sales = make_user("sales", role="Sales")

        self.client = Client.objects.create(name="Acme")
        self.other_client = Client.objects.create(name="Globex")
        self.project = Project.objects.create(
            client=self.client, name="Website", status=Project.Status.ACTIVE,
            start_date=MONDAY - timedelta(days=30), target_end_date=FRIDAY)
        self.other_project = Project.objects.create(
            client=self.other_client, name="Rebrand",
            status=Project.Status.ACTIVE)

        # Everyone in the scenario is on both projects. Row-level visibility is
        # tested in projects/tests_access.py; these tests are about the calendar,
        # and an empty calendar would hide what they are actually asserting.
        for project in (self.project, self.other_project):
            project.members.add(self.manager, self.pm, self.anna, self.bilal,
                                self.sales)

        # --- task due Monday ---
        self.task = Task.objects.create(
            project=self.project, title="Build header", assignee=self.anna,
            department=self.delivery, due_date=MONDAY,
            estimated_hours=Decimal("6"))

        # --- milestone due Wednesday ---
        self.milestone = Milestone.objects.create(
            project=self.project, title="Design signed off", due_date=WEDNESDAY)

        # --- document review due Thursday ---
        self.doc_type = DocumentType.objects.create(
            name="SRS", slug="srs", category=DocumentType.Category.DELIVERY)
        self.document = Document.objects.create(
            project=self.project, document_type=self.doc_type,
            title="Website SRS", status=Document.Status.REVIEW,
            review_due_date=THURSDAY, created_by=self.pm)
        # A financial document, for the gating tests. Its slug is one of
        # documents.access.FINANCIAL_DOC_MODULES.
        self.invoice_type = DocumentType.objects.create(
            name="Invoice", slug="invoice",
            category=DocumentType.Category.CLOSING)
        self.invoice = Document.objects.create(
            project=self.project, document_type=self.invoice_type,
            title="Invoice INV-0042", status=Document.Status.REVIEW,
            review_due_date=THURSDAY, created_by=self.pm)

        # --- a timed meeting on Tuesday ---
        self.meeting = CalendarEvent.objects.create(
            kind=CalendarEvent.Kind.MEETING, title="Client review",
            start_date=TUESDAY, start_time=time(14, 0), end_time=time(15, 0),
            project=self.project, department=self.delivery,
            location="Meet", created_by=self.pm, reminder_minutes=60)
        self.meeting.attendees.set([self.anna, self.pm])

        # --- a weekly recurring stand-up, Mondays, starting the 6th ---
        self.standup = CalendarEvent.objects.create(
            kind=CalendarEvent.Kind.EVENT, title="Weekly stand-up",
            start_date=MONDAY, start_time=time(9, 30), end_time=time(9, 45),
            frequency=CalendarEvent.Frequency.WEEKLY, interval=1,
            department=self.delivery, created_by=self.manager,
            reminder_minutes=15)
        self.standup.attendees.set([self.anna, self.bilal])

        # --- Bilal away Mon 13 to Wed 15 ---
        self.leave = LeaveRecord.objects.create(
            user=self.bilal, kind=LeaveRecord.Kind.VACATION,
            status=LeaveRecord.Status.APPROVED,
            start_date=MONDAY + timedelta(days=7),
            end_date=MONDAY + timedelta(days=9))

        # --- a company-wide holiday on Wed 15 ---
        self.holiday = LeaveRecord.objects.create(
            user=None, kind=LeaveRecord.Kind.HOLIDAY,
            status=LeaveRecord.Status.APPROVED,
            start_date=MONDAY + timedelta(days=9),
            end_date=MONDAY + timedelta(days=9), note="Founders' Day")

        # --- a client-facing Instagram reel, Thursday evening ---
        self.client_activity = ClientActivity.objects.create(
            project=self.project, kind=ClientActivity.Kind.REEL,
            platform=ClientActivity.Platform.INSTAGRAM, date=THURSDAY,
            time=time(18, 30), title="Launch reel")


def set_perm(role_name, module_key, action, allowed):
    """Flip one cell of the RBAC matrix, the way the settings screen would.

    Tests that change access go through the real matrix rather than monkey-
    patching `has_perm`, so what they prove is what an administrator would
    actually get by ticking the box.
    """
    from accounts.models import Module, Role, RolePermission

    role = Role.objects.get(name=role_name)
    module, _ = Module.objects.get_or_create(
        key=module_key, defaults={"label": module_key})
    row, _ = RolePermission.objects.get_or_create(role=role, module=module)
    setattr(row, f"can_{action}", allowed)
    row.save()


def forget_perms(*users):
    """Drop the per-instance RBAC cache after a matrix change.

    `has_perm` memoises onto the user object, which is right for a request but
    wrong for a test that flips a permission and then reuses the same instance —
    without this the assertion would read the answer from before the change.
    """
    for user in users:
        if hasattr(user, "_rbac_cache"):
            del user._rbac_cache


def a_private_event(owner, *, attendees=(), when=None):
    event = CalendarEvent.objects.create(
        title="One to one", start_date=when or THURSDAY,
        visibility=CalendarEvent.Visibility.PRIVATE, created_by=owner)
    if attendees:
        event.attendees.set(attendees)
    return event
