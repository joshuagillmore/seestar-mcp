"""Task 3 (2026-09-22 review remediation): the meteoblue API key must never reach
stderr/journald via httpx's request-line logging.

Importing :mod:`seestar_mcp.server` runs ``FastMCP("seestar-mcp")``, which calls
the ``mcp`` package's ``configure_logging("INFO")`` -- effectively
``logging.basicConfig(level="INFO", handlers=[RichHandler(stderr)])`` on the
ROOT logger. httpx logs every request at INFO as
``HTTP Request: GET <full-url> "HTTP/1.1 200 OK"``, and
:class:`~seestar_mcp.planning.weather.MeteoblueSource` carries
``SEESTAR_METEOBLUE_API_KEY`` as the ``apikey`` query parameter -- so every
keyed weather fetch used to write the key straight to stderr / journald, even
though ``provenance.redact`` already keeps it out of the provenance log (see
SECURITY.md). The fix has two independent layers, both exercised below:

1. ``server.py`` raises the ``httpx``/``httpcore`` loggers to WARNING right
   after ``mcp = FastMCP(...)``, so the INFO-level request-line log is never
   emitted in the first place.
2. A ``logging.Filter`` attached to the ``httpx`` logger redacts
   ``apikey=<value>`` from any record that *does* get through -- defense in
   depth against a future level change re-opening the leak.

``pytest``'s ``caplog.set_level(logging.DEBUG)`` only sets the ROOT logger's
level; it does not override a level set explicitly on the ``httpx`` logger,
which is exactly the point of layer 1. That also means a naive test asserting
"key not in caplog.text" could pass vacuously if caplog captured nothing at
all from this path. ``test_filter_redacts_even_if_the_level_gate_is_reopened``
is the positive control: it forces the ``httpx`` logger back to INFO (as if
layer 1 regressed) and asserts caplog DOES capture an httpx request record --
proving the capture path works -- while the key is still absent, proving layer
2 holds on its own.
"""

from __future__ import annotations

import logging

import httpx
import respx

import seestar_mcp.server  # noqa: F401 - import side effect: configures root logging
from seestar_mcp.planning.site import SiteProfile
from seestar_mcp.planning.weather import MeteoblueSource

_LEAK_KEY = "SUPERSECRETKEY123"
_SITE = SiteProfile(name="t", lat_deg=38.9, lon_deg=-77.0)
_WINDOW = ("2026-09-23T00:00:00+00:00", "2026-09-23T09:00:00+00:00")


def _mock_meteoblue() -> None:
    respx.get(url__startswith="https://my.meteoblue.com/packages/").mock(
        return_value=httpx.Response(200, json={"nope": True})
    )


@respx.mock
async def test_meteoblue_key_never_reaches_the_logs_under_default_config(caplog):
    """The production path: both layers active, as `import seestar_mcp.server` leaves them."""
    _mock_meteoblue()
    caplog.set_level(logging.DEBUG)  # root only -- see module docstring

    await MeteoblueSource(_LEAK_KEY).assess(_SITE, _WINDOW)

    assert _LEAK_KEY not in caplog.text


@respx.mock
async def test_filter_redacts_even_if_the_level_gate_is_reopened(caplog, monkeypatch):
    """Positive control: proves caplog captures this path, and layer 2 holds alone.

    Forces the httpx logger back to INFO (undoing layer 1) so its
    "HTTP Request: ..." record is actually emitted and captured -- if this
    assertion ever failed, the test above would be proving nothing. The key
    must still be absent, because the redaction filter (layer 2) is
    independent of the logger's level.
    """
    httpx_logger = logging.getLogger("httpx")
    monkeypatch.setattr(httpx_logger, "level", logging.INFO)
    caplog.set_level(logging.DEBUG)
    _mock_meteoblue()

    await MeteoblueSource(_LEAK_KEY).assess(_SITE, _WINDOW)

    # Non-vacuous: an httpx request record really was captured on this path.
    assert any(
        record.name == "httpx" and "HTTP Request" in record.getMessage()
        for record in caplog.records
    )
    # ... yet the key is not in it, because the filter redacts independently
    # of the level check that just let the record through.
    assert _LEAK_KEY not in caplog.text
