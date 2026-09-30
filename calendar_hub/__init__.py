"""Unified calendar.

Named `calendar_hub`, not `calendar`: the project root is on `sys.path`, so an
app package called `calendar` would shadow the standard library module of that
name — which `projects.models.RecurringTask._step` imports to clamp monthly
dates to the length of the month. The recurring-task engine would break the day
this app was added, in a way that looks nothing like a naming problem.
"""
