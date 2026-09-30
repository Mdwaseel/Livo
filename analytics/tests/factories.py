"""Shared fixture builder for the analytics tests.

One scenario with known-by-hand totals, so every assertion below can name the
number it expects instead of comparing the code to itself. If a test asserts
"18.0 hours", 18 is a figure a reader can add up from `ANNA_LOGS` here.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone

from accounts.models import Department, Role
from clients.models import Client
from documents.models import Document, DocumentType
from employees.models import EmployeeProfile
from finance.models import Payment
from projects.models import Milestone, Project, Task, WorkLogEntry

User = get_user_model()


def make_user(username, *, role=None, department=None, **extra):
    user = User.objects.create_user(
        username=username, email=f"{username}@livodigital.com",
        password="analytics-test-pass-99", **extra)
    if role:
        user.primary_role = Role.objects.get(name=role)
    if department:
        user.department = department
    user.save()
    # The employees app auto-creates a profile via post_save; make sure it is
    # ACTIVE so the selectors' resigned/terminated exclusion doesn't drop it.
    profile, _ = EmployeeProfile.objects.get_or_create(user=user)
    profile.status = EmployeeProfile.Status.ACTIVE
    profile.save()
    return user


class Scenario:
    """A small agency, fully populated, with totals worked out by hand.

    Anna  — 18 h logged (12 billable, 6 not), 2 tasks completed
    Bilal —  6 h logged (6 billable, 0 not),  1 task completed
    Project "Website"  budget 100000, received 40000  -> outstanding 60000
    Project "Branding" budget  50000, received     0  -> outstanding 50000
    """

    ANNA_TOTAL = Decimal("18")
    ANNA_BILLABLE = Decimal("12")
    BILAL_TOTAL = Decimal("6")
    TEAM_TOTAL = Decimal("24")
    TEAM_BILLABLE = Decimal("18")
    WEBSITE_BUDGET = Decimal("100000")
    WEBSITE_RECEIVED = Decimal("40000")
    BRANDING_BUDGET = Decimal("50000")
    PORTFOLIO_VALUE = Decimal("150000")
    PORTFOLIO_OUTSTANDING = Decimal("110000")

    def __init__(self):
        # Roles and the RBAC matrix arrive with accounts migration 0003, which
        # seeds every test database — nothing to set up here.
        self.today = timezone.localdate()
        self.delivery = Department.objects.create(name="Delivery")
        self.design = Department.objects.create(name="Design")

        self.manager = make_user("mgr", role="Manager", department=self.delivery)
        self.anna = make_user("anna", role="Developer", department=self.delivery)
        self.bilal = make_user("bilal", role="Designer", department=self.design)
        self.outsider = make_user("sales", role="Sales")

        self.client_a = Client.objects.create(name="Acme")
        self.client_b = Client.objects.create(name="Borealis")

        self.website = Project.objects.create(
            client=self.client_a, name="Website", status=Project.Status.ACTIVE,
            budget=self.WEBSITE_BUDGET,
            target_end_date=self.today - timedelta(days=5),  # deliberately late
        )
        self.branding = Project.objects.create(
            client=self.client_b, name="Branding", status=Project.Status.ACTIVE,
            budget=self.BRANDING_BUDGET,
            target_end_date=self.today + timedelta(days=30),
        )
        self.archived = Project.objects.create(
            client=self.client_a, name="Old thing", is_archived=True,
            budget=Decimal("999999"))

        # --- tasks ---
        self.done_a = Task.objects.create(
            project=self.website, title="Build header", assignee=self.anna,
            department=self.delivery, status=Task.Status.DONE,
            completed_on=self.today - timedelta(days=2),
            estimated_hours=Decimal("5"))
        self.done_b = Task.objects.create(
            project=self.website, title="Build footer", assignee=self.anna,
            department=self.delivery, status=Task.Status.DONE,
            completed_on=self.today - timedelta(days=1),
            estimated_hours=Decimal("3"))
        self.done_c = Task.objects.create(
            project=self.branding, title="Logo pass", assignee=self.bilal,
            department=self.design, status=Task.Status.DONE,
            completed_on=self.today, estimated_hours=Decimal("4"))
        self.overdue = Task.objects.create(
            project=self.website, title="Wire up forms", assignee=self.anna,
            department=self.delivery, status=Task.Status.IN_PROGRESS,
            due_date=self.today - timedelta(days=3), estimated_hours=Decimal("6"))
        self.due_today = Task.objects.create(
            project=self.website, title="Ship it", assignee=self.anna,
            department=self.delivery, status=Task.Status.TODO,
            due_date=self.today)

        # --- work logs (18 h Anna, 6 h Bilal) ---
        self._log(self.website, self.anna, days_ago=3, hours="8", billable=True,
                  approval=WorkLogEntry.Approval.APPROVED)
        self._log(self.website, self.anna, days_ago=2, hours="4", billable=True,
                  approval=WorkLogEntry.Approval.APPROVED)
        self._log(self.website, self.anna, days_ago=1, hours="6", billable=False,
                  approval=WorkLogEntry.Approval.SUBMITTED)
        self._log(self.branding, self.bilal, days_ago=1, hours="6", billable=True,
                  approval=WorkLogEntry.Approval.APPROVED)
        # Outside the default 90-day window — proves the date filter bites.
        self._log(self.website, self.anna, days_ago=400, hours="99", billable=True,
                  approval=WorkLogEntry.Approval.APPROVED)

        # --- milestones ---
        Milestone.objects.create(project=self.website, title="Design signoff",
                                 status=Milestone.Status.DONE)
        Milestone.objects.create(project=self.website, title="Launch",
                                 status=Milestone.Status.PENDING)

        # --- money ---
        Payment.objects.create(project=self.website, amount=self.WEBSITE_RECEIVED,
                               received_on=self.today - timedelta(days=10))
        # On an archived project: must never appear in any total.
        Payment.objects.create(project=self.archived, amount=Decimal("777777"),
                               received_on=self.today)

        # --- documents ---
        self.doc_type = DocumentType.objects.create(name="Brief", slug="brief")
        Document.objects.create(project=self.website, document_type=self.doc_type,
                                title="Scope")

    def _log(self, project, user, *, days_ago, hours, billable, approval):
        return WorkLogEntry.objects.create(
            project=project, logged_by=user,
            date=self.today - timedelta(days=days_ago),
            description="work", hours=Decimal(hours), is_billable=billable,
            approval_status=approval)
