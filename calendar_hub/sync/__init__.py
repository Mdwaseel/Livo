"""External calendar integration: the seam, and the one format that already
works everywhere.

`base.py` fixes the shape a Google/Outlook/Apple provider will have.
`ical.py` is a standards-compliant export those three can read today.
"""
from .base import (CalendarProvider, all_providers,  # noqa: F401
                   get_provider, register_provider)
from .ical import filename_for, invite_ics, to_ics  # noqa: F401
