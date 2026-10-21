"""A Flask test client that sends the dashboard's ``X-RC-Panel`` header on writes (#231).

Every page the dashboard serves sends ``X-RC-Panel`` on a POST, and ``_local_only``
refuses a write without it. Tests that exercise a write route as "the page would"
use ``panel_test_client``; a test about the missing header builds a raw
``app.test_client()`` on purpose. Not a test module (no ``test_`` prefix).
"""
from __future__ import annotations

from flask.testing import FlaskClient
from werkzeug.datastructures import Headers

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PANEL_HEADER = "X-RC-Panel"


class PanelClient(FlaskClient):
    """``FlaskClient`` that adds ``X-RC-Panel: test`` to a write unless the call sets it."""

    def open(self, *args, **kwargs):
        if str(kwargs.get("method") or "GET").upper() not in SAFE_METHODS:
            headers = Headers(kwargs.get("headers") or {})
            if PANEL_HEADER not in headers:
                headers[PANEL_HEADER] = "test"
            kwargs["headers"] = headers
        return super().open(*args, **kwargs)


def panel_test_client(app) -> PanelClient:
    return PanelClient(app, app.response_class, use_cookies=True)
