"""Fixture for the planner tests.

The window is pinned to a known week — Mon 6 to Sun 12 July 2026 — rather than
"today", because every capacity assertion depends on which days are weekends.
A fixture that drifts with the calendar would pass on a Tuesday and fail on a
Saturday, and the failure would look like a bug in the code.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model

from accounts.models import Department, Role
from clients.models import Client
from employees.models import EmployeeProfile
from projects.models import Project, Sprint, Task, WorkLogEntry
from resource_planner.models import CapacityProfile, LeaveRecord

User = get_user_model()

# A Monday. Everything below is expressed relative to it.
MONDAY = date(2026, 7, 6)
TUESDAY = MONDAY + timedelta(days=1)
WEDNESDAY = MONDAY + timedelta(days=2)
THURSDAY = MONDAY + timedelta(days=3)
FRIDAY = MONDAY + timedelta(days=4)
SATURDAY = MONDAY + timedelta(days=5)
SUNDAY = MONDAY + timedelta(days=6)


def make_user(username, *, role=None, department=None):
    user = User.objects.create_user(
        username=username, email=f"{username}@livodigital.com",
        password="planner-test-pass-99", first_name=username.title())
    if role:
        user.primary_role = Role.objects.get(name=role)
    if department:
        user.department = department
    user.save()
    profile, _ = EmployeeProfile.objects.get_or_create(user=user)
    profile.status = EmployeeProfile.Status.ACTIVE
    profile.save()
    return user


class Scenario:
    """Three people in a known week, with totals worked out by hand.

    Week of Mon 6 – Sun 12 July 2026. Default capacity is 8 h/day Mon–Fri = 40 h.

    Anna   — default capacity 40 h. 10 h logged, one open 6 h task due Wed
             with 2 h already logged against it → 4 h remaining.
             allocated = 10 logged + 4 remaining = 14 h.
    Bilal  — 6 h/day Mon–Fri = 30 h capacity. One open 40 h task due Thu,
             nothing logged → allocated 40 h. Overloaded.
    Carla  — default 40 h, on approved leave all week → capacity 0.
    """

    ANNA_CAPACITY = Decimal("40")
    ANNA_LOGGED = Decimal("10")
    ANNA_REMAINING = Decimal("4")
    ANNA_ALLOCATED = Decimal("14")

    BILAL_DAILY = Decimal("6")
    BILAL_CAPACITY = Decimal("30")
    BILAL_ALLOCATED = Decimal("40")

    def __init__(self):
        self.monday = MONDAY
        self.sunday = SUNDAY
        self.delivery = Department.objects.create(name="Delivery")
        self.design = Department.objects.create(name="Design")

        self.manager = make_user("mgr", role="Manager", department=self.delivery)
        self.pm = make_user("pm", role="Project Manager", department=self.delivery)
        self.anna = make_user("anna", role="Developer", department=self.delivery)
        self.bilal = make_user("bilal", role="Designer", department=self.design)
        self.carla = make_user("carla", role="Developer", department=self.design)
        self.outsider = make_user("sales", role="Sales")

        # Only the people being planned; managers/sales are staff too but the
        # assertions below name the three the numbers are worked out for.
        self.client = Client.objects.create(name="Acme")
        self.project = Project.objects.create(
            client=self.client, name="Website", status=Project.Status.ACTIVE)
        self.sprint = Sprint.objects.create(
            project=self.project, name="Sprint 1", is_active=True,
            start_date=MONDAY, end_date=SUNDAY)

        # --- Bilal works a 6-hour day ---
        CapacityProfile.objects.create(user=self.bilal,
                                       daily_hours=self.BILAL_DAILY)

        # --- Anna: 8 h logged Monday + 2 h against her open task on Tuesday ---
        self.anna_task = Task.objects.create(
            project=self.project, title="Build header", assignee=self.anna,
            department=self.delivery, sprint=self.sprint,
            due_date=WEDNESDAY, estimated_hours=Decimal("6"))
        WorkLogEntry.objects.create(
            project=self.project, logged_by=self.anna, date=MONDAY,
            description="setup", hours=Decimal("8"), is_billable=True)
        WorkLogEntry.objects.create(
            project=self.project, task=self.anna_task, logged_by=self.anna,
            date=TUESDAY, description="header", hours=Decimal("2"),
            is_billable=True)
        # sync_actual_hours runs on save, so anna_task.actual_hours is now 2.
        self.anna_task.refresh_from_db()

        # --- Bilal: one oversized open task, nothing logged ---
        self.bilal_task = Task.objects.create(
            project=self.project, title="Full rebrand", assignee=self.bilal,
            department=self.design, sprint=self.sprint,
            due_date=THURSDAY, estimated_hours=Decimal("40"))

        # --- Carla: away all week ---
        self.carla_leave = LeaveRecord.objects.create(
            user=self.carla, kind=LeaveRecord.Kind.VACATION,
            status=LeaveRecord.Status.APPROVED,
            start_date=MONDAY, end_date=SUNDAY)

        # --- an unassigned task for the drag-and-drop tests ---
        self.backlog_task = Task.objects.create(
            project=self.project, title="Write copy", due_date=FRIDAY,
            estimated_hours=Decimal("3"))

        # --- a done task, which must never count toward anything ---
        Task.objects.create(
            project=self.project, title="Old thing", assignee=self.anna,
            status=Task.Status.DONE, completed_on=MONDAY,
            due_date=MONDAY, estimated_hours=Decimal("99"))
