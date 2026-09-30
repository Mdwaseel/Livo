"""Source adapters — one per app the calendar reads from.

Importing this package registers every source. `apps.CalendarHubConfig.ready`
does that at startup so the registry is never half-populated.

The order below is the order the legend and the "what's on" lists are built in:
work you owe first, then things about people.
"""
from .base import (EventSource, all_sources, get_source,  # noqa: F401
                   register, sources_for, REGISTRY)

# Imported for their side effect — each module registers its source. Ruff/flake8
# would call these unused; they are the whole point of the file.
from . import tasks        # noqa: F401,E402
from . import milestones   # noqa: F401,E402
from . import projects     # noqa: F401,E402
from . import meetings     # noqa: F401,E402
from . import client_activity  # noqa: F401,E402
from . import documents    # noqa: F401,E402
from . import leave        # noqa: F401,E402
from . import birthdays    # noqa: F401,E402
