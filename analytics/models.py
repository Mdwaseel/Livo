"""No models — and that is the design, not an omission.

Every figure this app reports is derived at query time from data that already
exists: `projects.Task`, `projects.WorkLogEntry`, `projects.Milestone`,
`finance.Payment`, `clients.Client`, `documents.Document`,
`employees.EmployeeProfile` and `accounts.Department`.

A rollup/snapshot table was considered and rejected. It would have to be kept
in step with six writers (task status changes, work-log edits and approvals,
payment create/delete, milestone completion, project archival, document
creation) — and every one of those is a place the cache could silently drift
from the truth. Aggregating on read is a handful of indexed `GROUP BY`s over
tables measured in thousands of rows, which is far cheaper than being wrong.

If a dataset ever outgrows that, the fix is a materialised view or a cached
`services.*` return value keyed on the filter set — not a table the app has to
remember to update. `selectors.py` is deliberately the only place that touches
the ORM, so that swap stays a one-file change.

Django still needs this module to exist for the app to load.
"""
