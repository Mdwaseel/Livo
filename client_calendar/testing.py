"""Test helpers (deliberately not named test*.py, so the runner doesn't collect it)."""
import re

from django.test import Client


class ReviewClient(Client):
    """A test client that fills in the review pages' signed form key.

    A real browser gets the key from the page it just rendered; tests that post
    straight to a review URL would otherwise have to fetch and parse the page
    first. Posts that already carry a `form_key` — including deliberately bad
    ones — are left exactly as written.
    """

    def post(self, path, data=None, *args, **kwargs):
        match = re.match(r"^/r/([^/]+)/", str(path))
        if match and (data is None or isinstance(data, dict)) and "form_key" not in (data or {}):
            from .models import ClientReviewer
            from .views_review import form_key

            reviewer = ClientReviewer.objects.filter(token=match.group(1)).first()
            if reviewer is not None:
                data = {**(data or {}), "form_key": form_key(reviewer)}
        return super().post(path, data, *args, **kwargs)
