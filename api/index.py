"""Vercel entry point.

Vercel's Python runtime serves the WSGI callable named `app` from this file;
vercel.json rewrites every path here, so Django does all the routing (static
files included, through WhiteNoise).
"""
import os
import sys
from pathlib import Path

# The function runs with api/ as its base; the project root holds `config` and
# the apps.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

app = application = get_wsgi_application()
