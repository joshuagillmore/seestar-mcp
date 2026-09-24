"""The suite's own isolation guarantees (``tests/conftest.py``).

Pinned because the suite was not hermetic (2026-09-22 review): a goto test
stamped the REAL ``./data/run_state.json``, astropy downloaded IERS tables,
a guardrail test reached the live Open-Meteo API, and a bare ``Settings()``
read the developer's ``.env`` (which points at a real scope). Each test here
fails if the matching conftest guarantee is removed; the loopback one fails if
the network guard over-reaches and breaks asyncio or local servers.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
from pathlib import Path

import pytest
from astropy.utils import iers

from seestar_mcp.config import Settings

# TEST-NET-1 (RFC 5737): documentation-only, never routed. If the guard were
# missing, a connect here would just time out rather than reach anything.
UNROUTABLE = "192.0.2.1"
BLOCKED_UNROUTABLE = rf"network access blocked.*{re.escape(UNROUTABLE)}"


def test_cwd_is_the_tests_own_tmp_dir(tmp_path):
    # Relative defaults (./data, .env) must land in a throwaway dir, never the repo.
    assert Path.cwd().samefile(tmp_path)


def test_settings_see_neither_the_shell_env_nor_the_repo_dotenv():
    assert [k for k in os.environ if k.upper().startswith("SEESTAR_")] == []
    s = Settings()
    assert s.seestar_host == "127.0.0.1"
    assert s.seestar_image_root == ""
    assert s.meteoblue_api_key == ""


def test_astropy_never_downloads_iers_tables():
    assert iers.conf.auto_download is False
    # Without this the bundled table is refused once its predictions are 30
    # days old, which offline means: always, eventually.
    assert iers.conf.auto_max_age is None


def test_non_loopback_connect_is_blocked():
    with pytest.raises(RuntimeError, match=BLOCKED_UNROUTABLE):
        socket.create_connection((UNROUTABLE, 80), timeout=2)
    with socket.socket() as sock:
        sock.settimeout(2)
        with pytest.raises(RuntimeError, match=BLOCKED_UNROUTABLE):
            sock.connect_ex((UNROUTABLE, 80))


def test_non_loopback_asyncio_connect_is_blocked():
    # Separate from the sync case: on Windows the Proactor loop connects via
    # ConnectEx and never calls socket.connect.
    async def _connect():
        await asyncio.wait_for(asyncio.open_connection(UNROUTABLE, 80), timeout=2)

    with pytest.raises(RuntimeError, match=BLOCKED_UNROUTABLE):
        asyncio.run(_connect())


def test_dns_lookup_is_blocked():
    with pytest.raises(RuntimeError, match=r"network access blocked.*example\.invalid"):
        socket.getaddrinfo("example.invalid", 80)


def test_loopback_stays_reachable():
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        socket.create_connection(("127.0.0.1", port), timeout=5).close()

        async def _open():
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()

        asyncio.run(_open())
