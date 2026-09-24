"""Suite-wide isolation: every test runs offline, in a throwaway cwd, env-clean.

Added 2026-09-22 after a review found the suite was not hermetic: a goto test
stamped the REAL ``./data/run_state.json``, astropy downloaded IERS tables at
test time, a guardrail test reached the live Open-Meteo API (and would have
spent meteoblue credits with a key exported), and bare ``Settings()`` calls
read the developer's ``.env``, which points at a real scope. The guarantees are
pinned by ``tests/test_isolation.py``.
"""

from __future__ import annotations

import asyncio.proactor_events
import ipaddress
import os
import socket

import pytest
from astropy.utils import iers

# Astropy's bundled IERS-A table is enough for planning at sub-arcsecond level.
# auto_download=False alone is not enough: IERS_Auto refuses predictive values
# older than auto_max_age (30 d) whether or not it may download, so offline the
# bundled table fails for every obstime past its predictive start once that is
# a month old. iers_degraded_accuracy does not help; it only governs IERS-B.
iers.conf.auto_download = False
iers.conf.auto_max_age = None


class NetworkBlockedError(RuntimeError):
    """A test tried to leave the machine.

    A RuntimeError, not an OSError, so code that degrades gracefully on network
    failure cannot quietly absorb it and let a leaking test pass.
    """


def _blocked(what: str) -> NetworkBlockedError:
    return NetworkBlockedError(
        f"network access blocked in tests: {what}. Mock it instead "
        "(respx for httpx, monkeypatch for weather and device calls)."
    )


def _text(host: str | bytes) -> str:
    return host.decode("ascii", "replace") if isinstance(host, bytes) else host


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None  # a hostname


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    ip = _ip(host)
    return ip is not None and (getattr(ip, "ipv4_mapped", None) or ip).is_loopback


def _check_connect(family: int, address) -> None:
    # AF_UNIX and friends never leave the machine; only IP connects are judged.
    if family in (socket.AF_INET, socket.AF_INET6):
        host = _text(address[0])
        if not _is_loopback(host):
            raise _blocked(f"connect to {host}:{address[1]}")


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo
_real_proactor_sock_connect = asyncio.proactor_events.BaseProactorEventLoop.sock_connect


def _guarded_connect(self, address):
    _check_connect(self.family, address)
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    _check_connect(self.family, address)
    return _real_connect_ex(self, address)


async def _guarded_proactor_sock_connect(self, sock, address):
    # Windows' Proactor loop connects via ConnectEx, never socket.connect, so
    # asyncio (and so httpx) would slip past the two guards above.
    _check_connect(sock.family, address)
    return await _real_proactor_sock_connect(self, sock, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    # A DNS query is itself traffic. None, "" and IP literals resolve locally;
    # the connect guards judge where they point.
    if host:
        name = _text(host)
        if name.lower() != "localhost" and _ip(name) is None:
            raise _blocked(f"DNS lookup of {name!r}")
    return _real_getaddrinfo(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """Relative defaults (``./data``, ``.env``) resolve into a throwaway dir."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _no_seestar_env(monkeypatch):
    """The developer's shell cannot configure the code under test.

    Matched case-insensitively: pydantic-settings reads env names that way.
    """
    for name in list(os.environ):
        if name.upper().startswith("SEESTAR_"):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any non-loopback connect or DNS lookup raises NetworkBlockedError."""
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    monkeypatch.setattr(
        asyncio.proactor_events.BaseProactorEventLoop,
        "sock_connect",
        _guarded_proactor_sock_connect,
    )
