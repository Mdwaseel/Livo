"""Selector + service tests: allocation is right, and never counted twice."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase

from projects.models import Task, WorkLogEntry
from resource_planner import capacity as cap
from resource_planner import selectors, services
from resource_planner.models import CapacityProfile, LeaveRecord

from .factories import (FRIDAY, MONDAY, SUNDAY, THURSDAY, TUESDAY, WEDNESDAY,
                        Scenario, make_user)


def week():
    return services.Window(start=MONDAY, end=SUNDAY, mode="week")


class AllocationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def rows(self, window=None):
        return {row["user"].username: row
                for row in services.workload(window or week(), today=MONDAY)}

    # --- the double-counting rule ---

    def test_remaining_estimate_is_used_not_the_full_estimate(self):
        """Anna's task is estimated at 6 h with 2 h logged. Counting the whole
        estimate alongside the logged hours would bill those 2 h twice."""
        scheduled = selectors.scheduled_hours_by_user(
            MONDAY, SUNDAY, user_ids=[self.data.anna.pk])
        self.assertEqual(scheduled[self.data.anna.pk], Scenario.ANNA_REMAINING)

    def test_allocation_is_logged_plus_remaining(self):
        anna = self.rows()["anna"]
        self.assertEqual(anna["logged_hours"], Scenario.ANNA_LOGGED)
        self.assertEqual(anna["scheduled_hours"], Scenario.ANNA_REMAINING)
        self.assertEqual(anna["allocated_hours"], Scenario.ANNA_ALLOCATED)

    def test_a_task_over_its_estimate_contributes_no_remaining_work(self):
        """Negative remaining would credit somebody with free time they haven't
        got."""
        task = Task.objects.create(
            project=self.data.project, title="Overrun", assignee=self.data.anna,
            due_date=WEDNESDAY, estimated_hours=Decimal("2"))
        WorkLogEntry.objects.create(
            project=self.data.project, task=task, logged_by=self.data.anna,
            date=TUESDAY, description="lots", hours=Decimal("10"))
        task.refresh_from_db()
        scheduled = selectors.scheduled_hours_by_user(
            MONDAY, SUNDAY, user_ids=[self.data.anna.pk])
        # 4 h from the original task, 0 (not -8) from the overrun one.
        self.assertEqual(scheduled[self.data.anna.pk], Decimal("4"))

    def test_done_tasks_are_excluded(self):
        """The fixture parks a 99 h DONE task on Anna."""
        anna = self.rows()["anna"]
        self.assertEqual(anna["scheduled_hours"], Scenario.ANNA_REMAINING)

    # --- capacity ---

    def test_default_capacity_is_forty_hours_a_week(self):
        self.assertEqual(self.rows()["anna"]["capacity"], Decimal("40.00"))

    def test_configured_capacity_is_honoured(self):
        self.assertEqual(self.rows()["bilal"]["capacity"], Decimal("30.00"))

    def test_leave_removes_capacity(self):
        carla = self.rows()["carla"]
        self.assertEqual(carla["capacity"], Decimal("0.00"))
        self.assertEqual(carla["leave_hours"], Decimal("40.00"))

    def test_remaining_capacity_goes_negative_when_overbooked(self):
        bilal = self.rows()["bilal"]
        self.assertEqual(bilal["allocated_hours"], Scenario.BILAL_ALLOCATED)
        self.assertEqual(bilal["remaining_capacity"], Decimal("-10.00"))

    # --- indicators ---

    def test_indicator_colours(self):
        rows = self.rows()
        self.assertEqual(rows["anna"]["status"], cap.STATUS_AVAILABLE)   # 35%
        self.assertEqual(rows["bilal"]["status"], cap.STATUS_OVER)       # 133%
        self.assertEqual(rows["carla"]["status"], cap.STATUS_OFF)        # on leave

    def test_near_capacity_lands_in_the_yellow_band(self):
        Task.objects.create(project=self.data.project, title="Filler",
                            assignee=self.data.anna, due_date=THURSDAY,
                            estimated_hours=Decimal("18"))
        # 10 logged + 4 remaining + 18 = 32 of 40 = 80%
        anna = self.rows()["anna"]
        self.assertEqual(anna["utilization_percent"], 80.0)
        self.assertEqual(anna["status"], cap.STATUS_NEAR)

    # --- the forward-looking figures ---

    def test_sprint_hours_come_from_active_sprints_covering_today(self):
        rows = self.rows()
        self.assertEqual(rows["anna"]["sprint_hours"], Decimal("4.00"))
        self.assertEqual(rows["bilal"]["sprint_hours"], Decimal("40.00"))

    def test_a_closed_sprint_does_not_count(self):
        self.data.sprint.is_active = False
        self.data.sprint.save()
        self.assertEqual(self.rows()["anna"]["sprint_hours"], Decimal("0.00"))

    def test_a_stale_sprint_outside_today_does_not_count(self):
        """`is_active` alone would include a sprint somebody forgot to close."""
        self.data.sprint.end_date = MONDAY - timedelta(days=1)
        self.data.sprint.save()
        self.assertEqual(self.rows()["anna"]["sprint_hours"], Decimal("0.00"))

    def test_future_hours_cover_everything_due_after_today(self):
        """"Upcoming" starts tomorrow, not after the current window — so Anna's
        Wednesday task counts alongside the one next month."""
        Task.objects.create(project=self.data.project, title="Next month",
                            assignee=self.data.anna,
                            due_date=MONDAY + timedelta(days=40),
                            estimated_hours=Decimal("12"))
        # 4 h remaining on Wednesday's task + 12 h next month.
        self.assertEqual(self.rows()["anna"]["future_hours"], Decimal("16.00"))

    def test_future_hours_exclude_work_already_overdue(self):
        """Late work is reported separately; folding it into "upcoming" would
        make a backlog look like a plan."""
        Task.objects.create(project=self.data.project, title="Late",
                            assignee=self.data.anna,
                            due_date=MONDAY - timedelta(days=3),
                            estimated_hours=Decimal("7"))
        anna = self.rows()["anna"]
        self.assertEqual(anna["future_hours"], Scenario.ANNA_REMAINING)
        self.assertEqual(anna["overdue_hours"], Decimal("7.00"))

    def test_overdue_work_still_counts_against_the_current_period(self):
        """Otherwise the planner shows a comfortable number for somebody who is
        three weeks behind."""
        Task.objects.create(project=self.data.project, title="Late",
                            assignee=self.data.anna,
                            due_date=MONDAY - timedelta(days=10),
                            estimated_hours=Decimal("5"))
        anna = self.rows()["anna"]
        self.assertEqual(anna["overdue_hours"], Decimal("5.00"))
        self.assertEqual(anna["scheduled_hours"], Decimal("9.00"))  # 4 + 5

    def test_unestimated_open_tasks_are_counted_and_flagged(self):
        Task.objects.create(project=self.data.project, title="No size",
                            assignee=self.data.anna, due_date=WEDNESDAY)
        anna = self.rows()["anna"]
        self.assertEqual(anna["unestimated_tasks"], 1)
        # Contributes zero hours — which is exactly why the count is shown.
        self.assertEqual(anna["scheduled_hours"], Scenario.ANNA_REMAINING)

    # --- scope ---

    def test_resigned_staff_are_excluded(self):
        from employees.models import EmployeeProfile
        profile = self.data.carla.employee_profile
        profile.status = EmployeeProfile.Status.RESIGNED
        profile.save()
        self.assertNotIn("carla", self.rows())

    def test_department_filter(self):
        window = services.Window(start=MONDAY, end=SUNDAY,
                                 department_id=self.data.design.pk)
        self.assertEqual(set(self.rows(window)), {"bilal", "carla"})

    def test_employee_filter(self):
        window = services.Window(start=MONDAY, end=SUNDAY,
                                 user_id=self.data.anna.pk)
        self.assertEqual(list(self.rows(window)), ["anna"])

    def test_empty_scope_returns_no_rows_not_an_error(self):
        window = services.Window(start=MONDAY, end=SUNDAY, user_id=999999)
        self.assertEqual(services.workload(window), [])


class SummaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_summary_is_the_sum_of_the_rows(self):
        rows = services.workload(week(), today=MONDAY)
        summary = services.summarise(rows)
        self.assertEqual(summary["capacity"],
                         sum(row["capacity"] for row in rows))
        self.assertEqual(summary["allocated"],
                         sum(row["allocated_hours"] for row in rows))
        self.assertEqual(summary["people"], len(rows))

    def test_status_counts_add_up(self):
        rows = services.workload(week(), today=MONDAY)
        summary = services.summarise(rows)
        total = (summary["available_count"] + summary["near_count"]
                 + summary["over_count"] + summary["off_count"])
        self.assertEqual(total, len(rows))

    def test_empty_summary_does_not_divide_by_zero(self):
        summary = services.summarise([])
        self.assertEqual(summary["capacity"], Decimal("0.00"))
        self.assertEqual(summary["utilization_percent"], 0.0)


class BoardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def board(self):
        return services.availability_board(
            services.workload(week(), today=MONDAY))

    def test_free_near_and_over_are_separated(self):
        board = self.board()
        self.assertIn("anna", [row["user"].username for row in board["free"]])
        self.assertIn("bilal", [row["user"].username for row in board["over"]])

    def test_people_with_no_capacity_are_left_out_entirely(self):
        """Someone on leave all week belongs in none of the three lists."""
        names = [row["user"].username
                 for key in ("free", "near", "over", "unclear")
                 for row in self.board()[key]]
        self.assertNotIn("carla", names)

    def test_apparently_free_but_unsized_goes_to_unclear(self):
        """20% with fifteen unestimated tasks is not safe to load up."""
        Task.objects.create(project=self.data.project, title="No size",
                            assignee=self.data.anna, due_date=WEDNESDAY)
        board = self.board()
        self.assertIn("anna", [row["user"].username for row in board["unclear"]])
        self.assertNotIn("anna", [row["user"].username for row in board["free"]])

    def test_free_list_is_sorted_by_most_spare_capacity(self):
        spare = [row["remaining_capacity"] for row in self.board()["free"]]
        self.assertEqual(spare, sorted(spare, reverse=True))


class OverbookingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_report_finds_the_overloaded_person(self):
        report = services.overbooking_report(
            services.workload(week(), today=MONDAY))
        self.assertEqual(report["count"], 1)
        self.assertEqual(report["rows"][0]["user"], self.data.bilal)
        self.assertEqual(report["total_overrun"], Decimal("10.00"))

    def test_nobody_over_is_an_empty_report_not_a_crash(self):
        self.data.bilal_task.delete()
        report = services.overbooking_report(
            services.workload(week(), today=MONDAY))
        self.assertEqual(report["count"], 0)
        self.assertEqual(report["total_overrun"], Decimal("0.00"))

    def test_would_overbook_predicts_a_move(self):
        rows = {row["user"].username: row
                for row in services.workload(week(), today=MONDAY)}
        anna = rows["anna"]  # 14 of 40 h
        self.assertEqual(services.would_overbook(anna, 10)[0], False)
        over, excess = services.would_overbook(anna, 30)
        self.assertTrue(over)
        self.assertEqual(excess, Decimal("4.00"))


class GridTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def grid(self):
        return services.planner_grid(week(), today=MONDAY)

    def test_one_column_per_day(self):
        grid = self.grid()
        self.assertEqual(len(grid["days"]), 7)
        for row in grid["rows"]:
            self.assertEqual(len(row["cells"]), 7)

    def test_hours_land_on_the_right_day(self):
        rows = {row["user"].username: row for row in self.grid()["rows"]}
        cells = {cell["date"]: cell for cell in rows["anna"]["cells"]}
        self.assertEqual(cells[MONDAY]["logged"], Decimal("8.00"))
        self.assertEqual(cells[TUESDAY]["logged"], Decimal("2.00"))
        # The open task's remaining 4 h sit on its due date.
        self.assertEqual(cells[WEDNESDAY]["scheduled"], Decimal("4.00"))

    def test_weekends_are_marked_non_working_with_zero_capacity(self):
        rows = {row["user"].username: row for row in self.grid()["rows"]}
        cells = {cell["date"]: cell for cell in rows["anna"]["cells"]}
        self.assertFalse(cells[SUNDAY]["is_working_day"])
        self.assertEqual(cells[SUNDAY]["capacity"], Decimal("0.00"))

    def test_leave_days_carry_their_record(self):
        rows = {row["user"].username: row for row in self.grid()["rows"]}
        monday_cell = rows["carla"]["cells"][0]
        self.assertIsNotNone(monday_cell["leave"])
        self.assertEqual(monday_cell["capacity"], Decimal("0.00"))

    def test_heat_levels_stay_in_range(self):
        for row in self.grid()["rows"]:
            for cell in row["cells"]:
                self.assertIn(cell["heat"], range(5))

    def test_heat_is_zero_on_a_day_with_no_capacity(self):
        rows = {row["user"].username: row for row in self.grid()["rows"]}
        self.assertEqual(rows["carla"]["cells"][0]["heat"], 0)

    def test_row_totals_equal_the_sum_of_the_cells(self):
        for row in self.grid()["rows"]:
            self.assertEqual(row["allocated"],
                             sum(cell["allocated"] for cell in row["cells"]))

    def test_column_totals_equal_the_sum_of_the_column(self):
        grid = self.grid()
        for index, total in enumerate(grid["totals"]):
            self.assertEqual(
                total["allocated"],
                sum(row["cells"][index]["allocated"] for row in grid["rows"]))

    def test_tasks_are_attached_to_their_cell(self):
        rows = {row["user"].username: row for row in self.grid()["rows"]}
        cells = {cell["date"]: cell for cell in rows["anna"]["cells"]}
        self.assertIn(self.data.anna_task, cells[WEDNESDAY]["tasks"])

    def test_monthly_grid_spans_the_month(self):
        window = services.Window.for_month(MONDAY)
        grid = services.planner_grid(window, today=MONDAY)
        self.assertEqual(len(grid["days"]), 31)  # July


class ForecastTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_forecast_returns_one_entry_per_week(self):
        forecast = services.capacity_forecast(weeks=6, today=MONDAY)
        self.assertEqual(len(forecast), 6)
        self.assertTrue(forecast[0]["is_current"])

    def test_forecast_weeks_start_on_mondays(self):
        for week_row in services.capacity_forecast(weeks=4, today=MONDAY):
            self.assertEqual(week_row["start"].weekday(), 0)

    def test_forecast_agrees_with_the_planner_for_the_current_week(self):
        """A forecast using different arithmetic from the planner would be the
        first thing to contradict it."""
        forecast = services.capacity_forecast(weeks=1, today=MONDAY)[0]
        summary = services.summarise(services.workload(week(), today=MONDAY))
        self.assertEqual(forecast["capacity"], summary["capacity"])
        self.assertEqual(forecast["allocated"], summary["allocated"])

    def test_leave_shows_up_as_a_capacity_dip(self):
        """Week 1 is the clean baseline — week 0 already has Carla's leave in
        it, so comparing against week 0 would compare two equal dips."""
        later = MONDAY + timedelta(days=14)
        LeaveRecord.objects.create(
            user=self.data.anna, status=LeaveRecord.Status.APPROVED,
            start_date=later, end_date=later + timedelta(days=6))
        forecast = services.capacity_forecast(weeks=4, today=MONDAY)
        self.assertLess(forecast[2]["capacity"], forecast[1]["capacity"])
        self.assertEqual(forecast[1]["capacity"] - forecast[2]["capacity"],
                         Decimal("40.00"))

    def test_a_requested_leave_causes_no_dip(self):
        later = MONDAY + timedelta(days=14)
        LeaveRecord.objects.create(
            user=self.data.anna, status=LeaveRecord.Status.REQUESTED,
            start_date=later, end_date=later + timedelta(days=6))
        forecast = services.capacity_forecast(weeks=4, today=MONDAY)
        self.assertEqual(forecast[2]["capacity"], forecast[1]["capacity"])


class DepartmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def rows(self):
        return {row["name"]: row
                for row in services.department_workload(week(), today=MONDAY)}

    def test_department_total_is_the_sum_of_its_people(self):
        for row in self.rows().values():
            self.assertEqual(row["capacity"],
                             sum(member["capacity"] for member in row["members"]))

    def test_overloaded_members_are_counted(self):
        self.assertEqual(self.rows()["Design"]["over_count"], 1)

    def test_people_without_a_department_get_their_own_bucket(self):
        make_user("floater")
        self.assertIn("Unassigned", self.rows())


class LeadersTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_top_and_bottom_exclude_people_with_no_capacity(self):
        rows = services.workload(week(), today=MONDAY)
        leaders = services.utilization_leaders(rows)
        names = [row["user"].username
                 for row in leaders["top"] + leaders["bottom"]]
        self.assertNotIn("carla", names)

    def test_top_is_the_most_utilised(self):
        leaders = services.utilization_leaders(
            services.workload(week(), today=MONDAY))
        self.assertEqual(leaders["top"][0]["user"], self.data.bilal)


class ProjectAllocationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.data = Scenario()

    def test_members_carry_logged_and_outstanding_hours(self):
        rows = {row["project"].name: row for row in
                selectors.project_allocation_rows(MONDAY, SUNDAY)}
        members = {member["user"].username: member
                   for member in rows["Website"]["members"]}
        self.assertEqual(members["anna"]["logged"], Decimal("10"))
        self.assertEqual(members["anna"]["scheduled"], Decimal("4"))
        self.assertEqual(members["bilal"]["scheduled"], Decimal("40"))

    def test_projects_with_nobody_on_them_still_appear(self):
        """A project with no one assigned is the interesting case, not one to
        filter out."""
        from projects.models import Project
        Project.objects.create(client=self.data.client, name="Ghost",
                               status=Project.Status.ACTIVE)
        names = {row["project"].name
                 for row in selectors.project_allocation_rows(MONDAY, SUNDAY)}
        self.assertIn("Ghost", names)
