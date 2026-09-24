"""Unit tests for seestar_mcp.server (controller logic + MCP tool registration).

Covers: exactly-18-tool registration with the expected names, honest destructive
descriptions, that SecretStore is not wired into any tool signature, and that
controller methods convert an ``AlpacaError`` into ``{"ok": False, ...}`` rather
than raising.
"""

from __future__ import annotations

import asyncio

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

import seestar_mcp.server as server_mod
from seestar_mcp.alpaca_client import AlpacaError
from seestar_mcp.server import SeestarController, mcp

EXPECTED_TOOLS = {
    "connect_telescope",
    "get_status",
    "get_view_state",
    "goto_target",
    "start_stack",
    "stop_view",
    "run_autofocus",
    "get_focuser_position",
    "plate_solve",
    "set_filter",
    "set_dew_heater",
    "park",
    "shutdown",
    "list_subs",
    "download_subs",
    "qa_tier1",
    "qa_tier2",
    "get_run_state",
    "qa_session_report",
    "get_site_profile",
    "set_site_profile",
    "assess_conditions",
    "get_target_observability",
    "plan_targets",
    "list_projects",
    "get_project",
    "set_project_goal",
    "log_session_result",
    "recommend_projects",
    "simulate_night",
    "check_night_guardrails",
    "log_sky_result",
    "suggest_horizon_mask",
    "add_horizon_mask",
}

# Destructive / motion / side-effecting tools that MUST state their effect.
DESTRUCTIVE = {
    "goto_target",
    "park",
    "shutdown",
    "set_dew_heater",
    "start_stack",
    "stop_view",
}


async def test_exactly_33_tools_with_expected_names():
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert len(tools) == 34
    assert names == EXPECTED_TOOLS


async def test_destructive_tools_describe_side_effects():
    tools = {t.name: t for t in await mcp.list_tools()}
    for name in DESTRUCTIVE:
        desc = (tools[name].description or "").lower()
        assert desc, f"{name} has no description"
        # Each destructive tool must plainly signal an effect.
        assert any(
            token in desc
            for token in ("side effect", "terminates", "invalidates", "motion", "halts")
        ), f"{name} description does not state its side effect: {desc!r}"
    # shutdown must specifically call out ending the control link.
    assert "terminates the seestar_alp control link" in (
        tools["shutdown"].description or ""
    ).lower()


async def test_all_tools_have_descriptions():
    tools = await mcp.list_tools()
    for t in tools:
        assert t.description and t.description.strip(), f"{t.name} lacks a description"


def test_secretstore_not_in_any_tool_signature():
    # No tool wrapper may take a SecretStore (or any 'secret'-named) parameter.
    for name in EXPECTED_TOOLS:
        func = getattr(server_mod, name)
        sig = inspect.signature(func)
        for pname, param in sig.parameters.items():
            assert "secret" not in pname.lower()
            ann = str(param.annotation).lower()
            assert "secretstore" not in ann
    # SecretStore is not even imported into the server module namespace.
    assert "SecretStore" not in vars(server_mod)


def _controller_with_mock_alpaca(alpaca):
    return SeestarController(
        settings=_dummy_settings(),
        provenance=MagicMock(),  # ProvenanceLog is a SYNC api
        alpaca=alpaca,
        data=AsyncMock(),
        tier1=AsyncMock(),
    )


def _dummy_settings():
    from seestar_mcp.config import Settings

    return Settings()


async def test_connect_telescope_maps_alpaca_error_to_ok_false():
    alpaca = AsyncMock()
    alpaca.set_connected.side_effect = AlpacaError(1031, "InvalidOperation", "connected")
    ctrl = _controller_with_mock_alpaca(alpaca)
    result = await ctrl.connect_telescope()
    assert result["ok"] is False
    assert result["error_number"] == 1031
    assert "InvalidOperation" in result["error"]


def test_fresh_controller_has_session_state_attrs():
    # Regression: commit df15a67 inserted `_weather_cached` in the middle of
    # __init__, leaving `session_id`/`manifest`/`target` as unreachable code
    # after `_weather_cached`'s `return value` — a fresh controller never set
    # them (2026-09-22 review remediation, task 2).
    ctrl = _controller_with_mock_alpaca(AsyncMock())
    assert ctrl.session_id is None
    assert ctrl.manifest is None
    assert ctrl.target is None


async def test_qa_session_report_does_not_raise_on_a_fresh_controller():
    # Same regression as above: qa_session_report reads self.target, which did
    # not exist on a controller that had not yet run goto_target in-process
    # (e.g. right after a server restart), raising AttributeError.
    ctrl = _controller_with_mock_alpaca(AsyncMock())
    result = await ctrl.qa_session_report(paths=[])
    assert isinstance(result, dict)


async def test_goto_target_maps_alpaca_error_to_ok_false(tmp_path):
    from seestar_mcp.config import Settings

    alpaca = AsyncMock()
    alpaca.method_sync.side_effect = AlpacaError(1025, "ValueNotSet", "action")
    ctrl = SeestarController(
        settings=Settings(manifest_dir=tmp_path / "m"),
        provenance=MagicMock(),  # ProvenanceLog is a SYNC api
        alpaca=alpaca,
        data=AsyncMock(),
        tier1=AsyncMock(),
    )
    result = await ctrl.goto_target("M31", 10.68, 41.27, session_id="err-1")
    assert result["ok"] is False
    assert result["error_number"] == 1025


async def test_goto_target_native_error_maps_to_ok_false(tmp_path):
    # Live-test regression: the scope tunnels a native "Error: ..." result inside
    # an otherwise-ok Alpaca envelope. goto_target MUST NOT report ok:true (which
    # would let a run-session flow proceed to solve/stack on a phantom goto).
    from seestar_mcp.config import Settings

    alpaca = AsyncMock()
    alpaca.method_sync.return_value = {
        "method": "iscope_start_view",
        "params": {"mode": "star"},
        "result": "Error: Exceeded allotted wait time for result",
    }
    ctrl = SeestarController(
        settings=Settings(manifest_dir=tmp_path / "m"),
        provenance=MagicMock(),  # ProvenanceLog is a SYNC api
        alpaca=alpaca,
        data=AsyncMock(),
        tier1=AsyncMock(),
    )
    result = await ctrl.goto_target("M31", 10.68, 41.27, session_id="err-native")
    assert result["ok"] is False
    assert "Error" in result["error"]
    assert result["raw"]["result"].startswith("Error")
    # It must NOT signal a started/successful session.
    assert result.get("ok") is not True


async def test_goto_target_native_success_stays_ok_true(tmp_path):
    # A legit non-error native result must still read as ok:true (no over-trigger).
    from seestar_mcp.config import Settings

    alpaca = AsyncMock()
    alpaca.method_sync.return_value = {"status": "ok", "method": "iscope_start_view"}
    ctrl = SeestarController(
        settings=Settings(manifest_dir=tmp_path / "m"),
        provenance=MagicMock(),  # ProvenanceLog is a SYNC api
        alpaca=alpaca,
        data=AsyncMock(),
        tier1=AsyncMock(),
    )
    result = await ctrl.goto_target("M31", 10.68, 41.27, session_id="ok-native")
    assert result["ok"] is True
    assert result["session_id"] == "ok-native"


async def test_start_stack_native_error_maps_to_ok_false():
    alpaca = AsyncMock()
    alpaca.method_sync.return_value = "Error: cannot start stack"
    ctrl = _controller_with_mock_alpaca(alpaca)
    result = await ctrl.start_stack()
    assert result["ok"] is False
    assert "Error" in result["error"]


async def test_run_autofocus_native_error_maps_to_ok_false():
    alpaca = AsyncMock()
    alpaca.method_sync.return_value = {"result": "Error: autofocus failed"}
    ctrl = _controller_with_mock_alpaca(alpaca)
    result = await ctrl.run_autofocus()
    assert result["ok"] is False
    assert "Error" in result["error"]


async def test_park_native_error_maps_to_ok_false():
    alpaca = AsyncMock()
    alpaca.method_sync.return_value = "Error: park refused"
    ctrl = _controller_with_mock_alpaca(alpaca)
    result = await ctrl.park()
    assert result["ok"] is False
    assert "Error" in result["error"]


async def test_stop_view_native_error_maps_to_ok_false():
    alpaca = AsyncMock()
    alpaca.method_sync.return_value = {"result": "Error: stop failed"}
    ctrl = _controller_with_mock_alpaca(alpaca)
    result = await ctrl.stop_view("Stack")
    assert result["ok"] is False
    assert "Error" in result["error"]


async def test_set_filter_native_error_maps_to_ok_false():
    alpaca = AsyncMock()
    alpaca.method_sync.return_value = "Error: wheel jammed"
    ctrl = _controller_with_mock_alpaca(alpaca)
    result = await ctrl.set_filter(1)
    assert result["ok"] is False
    assert "Error" in result["error"]


async def test_set_dew_heater_native_error_maps_to_ok_false():
    alpaca = AsyncMock()
    alpaca.method_sync.return_value = {"result": "Error: heater fault"}
    ctrl = _controller_with_mock_alpaca(alpaca)
    result = await ctrl.set_dew_heater(True)
    assert result["ok"] is False
    assert "Error" in result["error"]


async def test_shutdown_native_error_maps_to_ok_false():
    alpaca = AsyncMock()
    alpaca.method_sync.return_value = "Error: shutdown refused"
    ctrl = _controller_with_mock_alpaca(alpaca)
    result = await ctrl.shutdown()
    assert result["ok"] is False
    assert "Error" in result["error"]


async def test_get_status_never_raises_on_error():
    alpaca = AsyncMock()
    alpaca.get_connected.side_effect = AlpacaError(1099, "boom", "connected")
    ctrl = _controller_with_mock_alpaca(alpaca)
    result = await ctrl.get_status()
    assert result["ok"] is False
    assert result["error_number"] == 1099


async def test_get_view_state_native_error_maps_to_ok_false():
    # When idle/slow, seestar_alp tunnels a native "Error: ..." result string
    # inside an otherwise-ok Alpaca envelope. Surface it as ok:false.
    alpaca = AsyncMock()
    alpaca.method_sync.return_value = {
        "method": "get_view_state",
        "params": [],
        "result": "Error: Exceeded allotted wait time for result",
    }
    ctrl = _controller_with_mock_alpaca(alpaca)
    result = await ctrl.get_view_state()
    assert result["ok"] is False
    assert "Error" in result["error"]
    assert result["raw"]["result"].startswith("Error")


async def test_plate_solve_native_error_string_maps_to_ok_false():
    alpaca = AsyncMock()
    alpaca.method_sync.return_value = "Error: solve failed"
    ctrl = _controller_with_mock_alpaca(alpaca)
    result = await ctrl.plate_solve()
    assert result["ok"] is False
    assert "Error" in result["error"]


async def test_get_focuser_position_native_error_maps_to_ok_false():
    alpaca = AsyncMock()
    alpaca.method_sync.return_value = {"result": "Error: no focuser"}
    ctrl = _controller_with_mock_alpaca(alpaca)
    result = await ctrl.get_focuser_position()
    assert result["ok"] is False


async def test_view_state_transport_error_returns_ok_false():
    from seestar_mcp.alpaca_client import AlpacaTransportError

    alpaca = AsyncMock()
    alpaca.method_sync.side_effect = AlpacaTransportError(
        -1, "Connection refused", "action"
    )
    ctrl = _controller_with_mock_alpaca(alpaca)
    result = await ctrl.get_view_state()  # must not raise
    assert result["ok"] is False
    assert result["error_number"] == -1


async def test_get_status_transport_error_returns_ok_false():
    from seestar_mcp.alpaca_client import AlpacaTransportError

    alpaca = AsyncMock()
    alpaca.get_connected.side_effect = AlpacaTransportError(
        -1, "Connection refused", "connected"
    )
    ctrl = _controller_with_mock_alpaca(alpaca)
    result = await ctrl.get_status()  # must not raise past method-level guard
    assert result["ok"] is False
    assert result["error_number"] == -1


# --- Regression tests for the 2026-07-12 live-session bugs ---


async def test_goto_target_sends_ra_in_hours():
    # HARDWARE: firmware's target_ra_dec wants RA in HOURS; the tool takes
    # catalog DEGREES and must divide by 15. Passing degrees = silent no-slew.
    alpaca = AsyncMock()
    alpaca.method_sync.return_value = {"result": 0}
    ctrl = _controller_with_mock_alpaca(alpaca)
    await ctrl.goto_target("M51", 202.4696, 47.1952, session_id="hrs")
    call = alpaca.method_sync.await_args
    assert call.args[0] == "iscope_start_view"
    ra_hours, dec = call.args[1]["target_ra_dec"]
    assert abs(ra_hours - 202.4696 / 15.0) < 1e-6  # RA -> hours
    assert abs(dec - 47.1952) < 1e-6               # Dec stays degrees


async def test_set_dew_heater_uses_pi_output_set2():
    # HARDWARE: heater is a power-output channel, not a set_setting key.
    alpaca = AsyncMock()
    alpaca.method_sync.return_value = {"result": 0}
    ctrl = _controller_with_mock_alpaca(alpaca)
    result = await ctrl.set_dew_heater(True)
    assert result["ok"] is True
    call = alpaca.method_sync.await_args
    assert call.args[0] == "pi_output_set2"
    assert call.args[1] == {"heater": {"state": True, "value": 90}}
    off = await ctrl.set_dew_heater(False)
    assert off["ok"] is True
    assert alpaca.method_sync.await_args.args[1] == {"heater": {"state": False, "value": 0}}


def test_parse_device_health_reads_nested_is_verified():
    from seestar_mcp.server import _parse_device_health

    # Real shape: is_verified nested under result.device (NOT top level).
    dev = {"result": {"device": {"is_verified": True}, "setting": {}}}
    assert _parse_device_health(dev) == (True, True)
    # Fail-safe on empty/malformed.
    assert _parse_device_health({}) == (False, False)
    assert _parse_device_health(None) == (False, False)
    # Flat fallback for simple mocks.
    assert _parse_device_health({"is_verified": True}) == (True, True)
    # Nested but unverified.
    assert _parse_device_health({"result": {"device": {"is_verified": False}}}) == (True, False)


def test_parse_battery_reads_pi_get_info():
    from seestar_mcp.server import _parse_battery

    assert _parse_battery({"result": {"battery_capacity": 82}}) == 82.0
    assert _parse_battery({}) is None
    assert _parse_battery(None) is None
    assert _parse_battery({"battery_capacity": 55}) == 55.0  # flat fallback
    assert _parse_battery({"result": {"battery_capacity": True}}) is None  # bool != pct


def test_readonly_tools_log_under_their_own_names(tmp_path):
    """get_status / get_view_state / get_focuser_position must appear in the log.

    They previously produced only transport-level records, so a consumer reading
    the log saw calls it could not attribute to any tool it had invoked — its own
    traffic fanned out under names outside its allowlist.
    """
    import asyncio
    import json as _json
    from unittest.mock import AsyncMock, MagicMock

    from seestar_mcp.config import Settings
    from seestar_mcp.provenance import ProvenanceLog
    from seestar_mcp.server import SeestarController

    p = tmp_path / "prov.jsonl"
    alpaca = AsyncMock()
    alpaca.method_sync.return_value = {"result": {}}
    c = SeestarController(
        settings=Settings(_env_file=None, data_dir=tmp_path),
        provenance=ProvenanceLog(p, client_id="agent"),
        alpaca=alpaca,
        data=MagicMock(),
        tier1=MagicMock(),
    )
    asyncio.run(c.get_status())
    asyncio.run(c.get_view_state())
    asyncio.run(c.get_focuser_position())

    tools = [_json.loads(line)["tool"] for line in p.read_text().splitlines()]
    assert {"get_status", "get_view_state", "get_focuser_position"} <= set(tools)


# --- focuser position: unwrap the JSON-RPC envelope --------------------------
# Found 2026-08-03 during the firmware 8.46 check. The device answers
# get_focuser_position with the standard envelope and the value nested under
# "result", but _extract_focus_pos only looked at the top level, so a live scope
# reporting step 1534 came back as focus_pos: null. _parse_gps already unwraps
# "result"; this helper did not. Also silently starved run_autofocus, which uses
# the same helper to seed the Tier-1 focus-drift baseline.


def test_extract_focus_pos_unwraps_the_result_envelope():
    """The real fw 8.46 reply, verbatim from hardware."""
    from seestar_mcp.server import _extract_focus_pos

    reply = {
        "jsonrpc": "2.0",
        "Timestamp": "386.625599726",
        "method": "get_focuser_position",
        "result": {"step": 1534},
        "code": 0,
        "id": 90293,
    }
    assert _extract_focus_pos(reply) == 1534


def test_extract_focus_pos_still_accepts_the_flat_shapes():
    """REGRESSION: bare ints and top-level keys must keep working."""
    from seestar_mcp.server import _extract_focus_pos

    assert _extract_focus_pos(1200) == 1200
    assert _extract_focus_pos({"step": 900}) == 900
    assert _extract_focus_pos({"focus_pos": 42}) == 42
    assert _extract_focus_pos({"result": 777}) == 777  # result as a bare number
    assert _extract_focus_pos({}) is None
    assert _extract_focus_pos(None) is None
    assert _extract_focus_pos({"result": {"nothing": 1}}) is None


# --- weather fetches are cached (the 2026-07-31 credit burn) -----------------
# A dashboard polled check_night_guardrails once a minute for 11 hours straight
# through dawn: 951 uncached forecast fetches in one day, against 1-5 on every
# other day, ~8M meteoblue credits. The guardrail consumes exactly one value
# from the whole forecast — the tri-state weather_go.


def _ctl(tmp_path, ttl=900.0):
    from unittest.mock import AsyncMock, MagicMock

    from seestar_mcp.config import Settings
    from seestar_mcp.server import SeestarController

    return SeestarController(
        settings=Settings(_env_file=None, data_dir=tmp_path, weather_cache_ttl_s=ttl),
        provenance=MagicMock(), alpaca=AsyncMock(),
        data=AsyncMock(), tier1=AsyncMock(),
    )


def test_repeated_guardrail_polls_issue_one_weather_fetch(tmp_path, monkeypatch):
    """60 polls in a TTL window must cost ONE upstream forecast."""
    from unittest.mock import MagicMock

    from seestar_mcp import server as srv

    calls = {"n": 0}

    async def counting(site, window, illum, **kw):
        calls["n"] += 1
        return MagicMock(go=True)

    monkeypatch.setattr(srv, "assess_conditions_weather", counting)
    c = _ctl(tmp_path)
    site = MagicMock(lat_deg=45.4, lon_deg=-75.7)
    for _ in range(60):
        asyncio.run(c._weather_cached(site, ("a", "b"), 0.0))
    assert calls["n"] == 1, f"{calls['n']} upstream fetches for 60 polls — cache not applied"


def test_cache_refetches_when_the_scope_moves(tmp_path, monkeypatch):
    """A real change (new site or new night) must not serve a stale forecast."""
    from unittest.mock import MagicMock

    from seestar_mcp import server as srv

    calls = {"n": 0}

    async def counting(site, window, illum, **kw):
        calls["n"] += 1
        return MagicMock(go=True)

    monkeypatch.setattr(srv, "assess_conditions_weather", counting)
    c = _ctl(tmp_path)
    a = MagicMock(lat_deg=45.4, lon_deg=-75.7)
    b = MagicMock(lat_deg=44.0, lon_deg=-79.4)          # scope moved
    asyncio.run(c._weather_cached(a, ("n1", "n2"), 0.0))
    asyncio.run(c._weather_cached(a, ("n1", "n2"), 0.0))  # cached
    asyncio.run(c._weather_cached(b, ("n1", "n2"), 0.0))  # different site
    asyncio.run(c._weather_cached(a, ("n3", "n4"), 0.0))  # different night
    assert calls["n"] == 3, f"expected 3 fetches (a, b, new-window), got {calls['n']}"


def test_ttl_zero_disables_caching(tmp_path, monkeypatch):
    """The escape hatch must actually bypass the cache."""
    from unittest.mock import MagicMock

    from seestar_mcp import server as srv

    calls = {"n": 0}

    async def counting(site, window, illum, **kw):
        calls["n"] += 1
        return MagicMock(go=True)

    monkeypatch.setattr(srv, "assess_conditions_weather", counting)
    c = _ctl(tmp_path, ttl=0.0)
    site = MagicMock(lat_deg=45.4, lon_deg=-75.7)
    for _ in range(3):
        asyncio.run(c._weather_cached(site, ("a", "b"), 0.0))
    assert calls["n"] == 3


def test_cache_survives_a_drifting_dark_window(tmp_path, monkeypatch):
    """REGRESSION: the window is recomputed from `now`, so its bounds drift.

    The first version of this cache keyed on the raw window tuple. Because
    dark_window() is derived from the current time, its bounds move by seconds
    on every call, so the key never repeated and the cache never hit — measured
    at 10 upstream fetches for 10 polls even though the unit test (which passed
    a fixed tuple) was green. Keying truncates to the hour.
    """
    from unittest.mock import MagicMock

    from seestar_mcp import server as srv

    calls = {"n": 0}

    async def counting(site, window, illum, **kw):
        calls["n"] += 1
        return MagicMock(go=True)

    monkeypatch.setattr(srv, "assess_conditions_weather", counting)
    c = _ctl(tmp_path)
    site = MagicMock(lat_deg=45.4, lon_deg=-75.7)
    # Same night, bounds drifting by seconds — exactly what the live path does.
    for sec in range(0, 40, 4):
        window = (f"2026-08-04T02:35:{sec:02d}.123456", f"2026-08-04T07:45:{sec:02d}.123456")
        asyncio.run(c._weather_cached(site, window, 0.71))
    assert calls["n"] == 1, (
        f"{calls['n']} fetches for a drifting-but-identical window — the cache "
        "key is time-sensitive again"
    )


# --- connect_telescope must not report success on a failed connection --------
# Found by exercising every tool surface against an unreachable scope, and
# reproduced live on 2026-08-08: the tool returned {"ok": true, "connected":
# false}. `ok` meant "the call ran" and the truth sat in `connected`, so anything
# branching on `ok` alone — a run-book, a dashboard — concluded the scope was
# connected when it was not. For an ACTION tool named connect_*, failing to
# connect is a failed action.


def test_connect_telescope_reports_failure_when_it_does_not_connect(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from seestar_mcp.config import Settings
    from seestar_mcp.server import SeestarController

    alpaca = AsyncMock()
    alpaca.set_connected.return_value = None
    alpaca.get_connected.return_value = False          # the scope stayed down
    c = SeestarController(
        settings=Settings(_env_file=None, data_dir=tmp_path),
        provenance=MagicMock(), alpaca=alpaca, data=AsyncMock(), tier1=AsyncMock(),
    )
    out = asyncio.run(c.connect_telescope())

    assert out["ok"] is False, "reported success while the scope is not connected"
    assert out["connected"] is False, "the connected field must still be carried"
    assert out.get("error"), "ok:false must carry a reason"


def test_connect_telescope_still_succeeds_when_it_connects(tmp_path):
    """REGRESSION: the happy path must be unchanged."""
    from unittest.mock import AsyncMock, MagicMock

    from seestar_mcp.config import Settings
    from seestar_mcp.server import SeestarController

    alpaca = AsyncMock()
    alpaca.set_connected.return_value = None
    alpaca.get_connected.return_value = True
    c = SeestarController(
        settings=Settings(_env_file=None, data_dir=tmp_path),
        provenance=MagicMock(), alpaca=alpaca, data=AsyncMock(), tier1=AsyncMock(),
    )
    out = asyncio.run(c.connect_telescope())
    assert out["ok"] is True and out["connected"] is True


# --- native JSON-RPC error dicts must fail (2026-09-22 review, task 5) --------
# The firmware reports a rejected command as a JSON-RPC dict,
# {"error": "method not found", "code": 103} (probed on fw 8.46, see
# data_client.py). _native_error only recognised an "Error..." *string*, so that
# dict passed as success: park/goto/stack/filter/heater/shutdown all returned
# ok:true on a command the scope never ran, and park() then cleared the run
# state although the mount never folded.

NATIVE_ERROR_REPLY = {"error": "method not found", "code": 103}
NATIVE_OK_REPLY = {"jsonrpc": "2.0", "method": "x", "result": 0, "code": 0}

#: Every controller method that routes a native result through _native_fail.
_NATIVE_GUARDED = {
    "get_view_state": lambda c: c.get_view_state(),
    "goto_target": lambda c: c.goto_target("M31", 10.68, 41.27, session_id="t5"),
    "start_stack": lambda c: c.start_stack(),
    "stop_view": lambda c: c.stop_view("Stack"),
    "run_autofocus": lambda c: c.run_autofocus(),
    "get_focuser_position": lambda c: c.get_focuser_position(),
    "plate_solve": lambda c: c.plate_solve(),
    "set_filter": lambda c: c.set_filter(2),
    "set_dew_heater": lambda c: c.set_dew_heater(True),
    "park": lambda c: c.park(),
    "shutdown": lambda c: c.shutdown(),
}


def _native_ctl(tmp_path, reply):
    from seestar_mcp.config import Settings

    alpaca = AsyncMock()
    alpaca.method_sync.return_value = reply
    return SeestarController(
        settings=Settings(
            _env_file=None, data_dir=tmp_path, manifest_dir=tmp_path / "m"
        ),
        provenance=MagicMock(),
        alpaca=alpaca,
        data=AsyncMock(),
        tier1=MagicMock(),  # set_focus_baseline is sync
    )


def test_native_guarded_list_covers_every_native_fail_call_site():
    # "Every" must stay true: a new controller method that guards a native
    # result has to join the parametrization below, not silently skip it.
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(SeestarController)))
    callers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_native_fail"
            for n in ast.walk(fn)
        )
    }
    assert callers == set(_NATIVE_GUARDED)


@pytest.mark.parametrize("name", sorted(_NATIVE_GUARDED))
async def test_native_error_dict_maps_to_ok_false(tmp_path, name):
    ctrl = _native_ctl(tmp_path, dict(NATIVE_ERROR_REPLY))
    result = await _NATIVE_GUARDED[name](ctrl)
    assert result["ok"] is False, f"{name} reported success on a rejected command"
    assert "method not found" in result["error"]
    assert "103" in result["error"], "the firmware's code must reach the caller"


@pytest.mark.parametrize("name", sorted(_NATIVE_GUARDED))
async def test_native_success_with_code_zero_stays_ok_true(tmp_path, name):
    # No over-trigger: a normal reply carries "code": 0 and no "error" key.
    ctrl = _native_ctl(tmp_path, dict(NATIVE_OK_REPLY))
    result = await _NATIVE_GUARDED[name](ctrl)
    assert result["ok"] is True, f"{name} failed a normal success reply: {result}"


def test_native_error_formats_text_and_code():
    from seestar_mcp.server import _native_error

    assert _native_error(NATIVE_ERROR_REPLY) == "method not found (code 103)"
    assert _native_error({"error": "busy"}) == "busy"  # no code -> text only
    # Standard JSON-RPC 2.0 error object, in case a bridge normalises to it.
    assert _native_error(
        {"error": {"code": -32601, "message": "Method not found"}}
    ) == "Method not found (code -32601)"
    # Falsy "error" values and plain success replies are not errors.
    assert _native_error({"error": None, "result": 0, "code": 0}) is None
    assert _native_error({"error": "", "result": 0}) is None
    assert _native_error(NATIVE_OK_REPLY) is None
    # Existing shapes are unchanged.
    assert _native_error("Error: park refused") == "Error: park refused"
    assert _native_error({"result": "Error: x"}) == "Error: x"
    assert _native_error({"result": {"step": 1}}) is None


# --- code 0 with a truthy "error" text is SUCCESS (live test 2026-09-24) -----
# The dew heater's native pi_output_set2 answered every toggle with a truthy
# "error" string AND code 0, while it DID apply the change -- verified live by
# switching the heater off/on and reading get_device_state's
# result.setting.heater_enable flip each time. _native_error treated any
# truthy "error" as failure regardless of code, so set_dew_heater reported a
# false ok:false on a command that had actually worked. Real failures carry a
# nonzero code ("method not found" is 103, "no solve data" is 215, "fail to
# operate" is 207); a MISSING code is not proof of success either -- only an
# explicit code 0 is.

#: Captured verbatim, live test 2026-09-24.
NATIVE_WARNING_REPLY = {
    "jsonrpc": "2.0",
    "Timestamp": "663.578618899",
    "method": "pi_output_set2",
    "error": "expected object param",
    "code": 0,
    "result": 0,
    "id": 10104,
}


def test_native_error_code_zero_with_error_text_is_success():
    from seestar_mcp.server import _native_error

    assert _native_error(NATIVE_WARNING_REPLY) is None


async def test_set_dew_heater_code_zero_error_text_is_ok_with_warning(tmp_path):
    ctrl = _native_ctl(tmp_path, dict(NATIVE_WARNING_REPLY))
    result = await ctrl.set_dew_heater(True)
    assert result["ok"] is True
    assert "expected object param" in result["warning"]


@pytest.mark.parametrize("name", sorted(_NATIVE_GUARDED))
async def test_native_success_with_warning_text_carries_it_on_every_guarded_method(
    tmp_path, name
):
    # "Every controller method that uses _native_fail should carry `warning`
    # when present" -- not just set_dew_heater.
    ctrl = _native_ctl(tmp_path, dict(NATIVE_WARNING_REPLY))
    result = await _NATIVE_GUARDED[name](ctrl)
    assert result["ok"] is True, f"{name} failed a code-0 reply with error text: {result}"
    assert result.get("warning") == "expected object param", (
        f"{name} dropped the firmware's warning text: {result}"
    )


@pytest.mark.parametrize(
    "code",
    [103, 207, 215],
    ids=["method_not_found", "fail_to_operate", "no_solve_data"],
)
def test_native_error_nonzero_codes_still_fail(code):
    from seestar_mcp.server import _native_error

    assert _native_error({"error": "x", "code": code}) is not None


def test_native_error_missing_code_is_not_proof_of_success():
    # A missing "code" key must NOT be read as success -- only an explicit 0 is.
    from seestar_mcp.server import _native_error

    assert _native_error({"error": "heater fault", "result": 0}) == "heater fault"


async def test_park_rejected_by_device_keeps_the_run_state(tmp_path):
    from seestar_mcp.run_state import RunState, read_run_state, write_run_state

    ctrl = _native_ctl(tmp_path, dict(NATIVE_ERROR_REPLY))
    path = ctrl._run_state_path()
    write_run_state(
        RunState(session_start_utc="2026-09-22T02:00:00+00:00", target="M31"), path
    )
    result = await ctrl.park()
    assert result["ok"] is False
    assert path.exists(), "a rejected park cleared the run state; the mount never folded"
    assert read_run_state(path)["state"] == "active"


async def test_park_success_clears_the_run_state(tmp_path):
    from seestar_mcp.run_state import RunState, read_run_state, write_run_state

    ctrl = _native_ctl(
        tmp_path, {"jsonrpc": "2.0", "method": "scope_park", "result": 0, "code": 0}
    )
    path = ctrl._run_state_path()
    write_run_state(
        RunState(session_start_utc="2026-09-22T02:00:00+00:00", target="M31"), path
    )
    result = await ctrl.park()
    assert result["ok"] is True
    assert not path.exists()
    assert read_run_state(path)["state"] == "idle"


# Pin the native method each motion tool sends. These are FIRMWARE-DEPENDENT
# names; a silent rename would make the tool a no-op on hardware.
@pytest.mark.parametrize(
    ("invoke", "expected_args"),
    [
        (lambda c: c.park(), ("scope_park",)),
        (lambda c: c.shutdown(), ("pi_shutdown",)),
        (lambda c: c.set_filter(2), ("set_wheel_position", [2])),
        (lambda c: c.start_stack(), ("iscope_start_stack",)),
        (lambda c: c.stop_view("Stack"), ("iscope_stop_view", ["Stack"])),
        (
            lambda c: c.stop_view("ContinuousExposure"),
            ("iscope_stop_view", ["ContinuousExposure"]),
        ),
    ],
    ids=["park", "shutdown", "set_filter", "start_stack", "stop_view", "stop_view_ce"],
)
async def test_motion_tools_send_the_pinned_native_method(
    tmp_path, invoke, expected_args
):
    ctrl = _native_ctl(tmp_path, dict(NATIVE_OK_REPLY))
    await invoke(ctrl)
    assert ctrl.alpaca.method_sync.await_count == 1
    assert ctrl.alpaca.method_sync.await_args.args == expected_args
    assert ctrl.alpaca.method_sync.await_args.kwargs == {}


async def test_run_autofocus_sends_start_auto_focus_then_reads_the_focuser(tmp_path):
    ctrl = _native_ctl(tmp_path, dict(NATIVE_OK_REPLY))
    await ctrl.run_autofocus()
    calls = ctrl.alpaca.method_sync.await_args_list
    assert calls[0].args == ("start_auto_focus",)
    assert calls[1].args == ("get_focuser_position", {"ret_obj": True})


# --- plate_solve must not report a stale solve (2026-09-22 final review, F1) --
# plate_solve discarded start_solve's reply. If the scope rejected the solve and
# get_solve_result then returned the PREVIOUS solve, plate_solve answered ok:true
# with a stale solution — and plate_solve backs "never stack on a failed solve".
# The parametrized native-error test above could not see it: its mock rejects
# BOTH calls, so get_solve_result's own error failed the tool.

#: What get_solve_result hands back after a rejected start_solve: the last
#: solve the scope completed, for some other field.
_SOLVE_REPLY = {
    "jsonrpc": "2.0",
    "method": "get_solve_result",
    "result": {"ra_dec": [5.5881, -5.3911], "fov": [0.71, 1.27], "focal_len": 250},
    "code": 0,
}


def _solve_ctl(tmp_path, start_reply):
    ctrl = _native_ctl(tmp_path, None)
    replies = {"start_solve": start_reply, "get_solve_result": _SOLVE_REPLY}
    ctrl.alpaca.method_sync.side_effect = lambda method, *a, **k: replies[method]
    return ctrl


@pytest.mark.parametrize(
    "start_reply",
    [
        {"error": "fail to operate", "code": 207},
        dict(NATIVE_ERROR_REPLY),
    ],
    ids=["fail_to_operate_207", "method_not_found_103"],
)
async def test_plate_solve_fails_when_only_start_solve_is_rejected(
    tmp_path, start_reply
):
    ctrl = _solve_ctl(tmp_path, start_reply)
    result = await ctrl.plate_solve()
    assert result["ok"] is False, "a rejected start_solve reported a stale solve"
    assert start_reply["error"] in result["error"]
    assert str(start_reply["code"]) in result["error"]
    # The stale solution must not reach the caller in any field.
    assert "solve_result" not in result
    assert "5.5881" not in repr(result)
    # And it is never even read: nothing after a rejected start is trustworthy.
    methods = [c.args[0] for c in ctrl.alpaca.method_sync.await_args_list]
    assert methods == ["start_solve"]


async def test_plate_solve_fails_on_a_start_solve_timeout_string(tmp_path):
    # seestar_alp's own "Exceeded allotted wait time" string means the device
    # never acknowledged start_solve, so a following get_solve_result may be the
    # previous solve. Every other command already treats this string as fatal
    # (goto_target: "the scope did NOT start the view"), so start_solve does too.
    ctrl = _solve_ctl(tmp_path, "Error: Exceeded allotted wait time for result")
    result = await ctrl.plate_solve()
    assert result["ok"] is False
    assert "Exceeded allotted wait time" in result["error"]
    assert "solve_result" not in result


async def test_plate_solve_accepted_start_returns_the_solve(tmp_path):
    # No over-trigger: an acknowledged start_solve still reads and returns the
    # solve, and the call order is pinned (start, then read).
    ctrl = _solve_ctl(
        tmp_path, {"jsonrpc": "2.0", "method": "start_solve", "result": 0, "code": 0}
    )
    result = await ctrl.plate_solve()
    assert result["ok"] is True
    assert result["solve_result"] == _SOLVE_REPLY
    methods = [c.args[0] for c in ctrl.alpaca.method_sync.await_args_list]
    assert methods == ["start_solve", "get_solve_result"]


# --- plate_solve polls through "no solve data" (task 2, live test 2026-09-24) -
# plate_solve called get_solve_result immediately after start_solve. On fw 8.46
# the device answered {"error": "no solve data", "code": 215} because the solve
# had not finished, and plate_solve returned a false ok:false on a solve that
# was still running. Poll instead, and only inside this loop does code 215 mean
# "in progress" rather than an error.

#: Captured verbatim (live test 2026-09-24): the "still solving" reply.
_SOLVE_215_REPLY = {
    "jsonrpc": "2.0",
    "Timestamp": "10925.804433318",
    "method": "get_solve_result",
    "error": "no solve data",
    "code": 215,
    "id": 13769,
}

#: Captured ra_dec/fov/angle values are M1's solved position (live test
#: 2026-09-24), not the observing site, so they are fine to commit. focal_len
#: matches the pre-existing _SOLVE_REPLY fixture above (same telescope).
#: image_id/state/star_number/duration_ms are synthetic -- only the keys, not
#: their values, were captured live.
_SOLVED_846_REPLY = {
    "jsonrpc": "2.0",
    "Timestamp": "10931.221",
    "method": "get_solve_result",
    "result": {
        "ra_dec": [5.572092, 22.068264],
        "fov": [0.712755, 1.269035],
        "focal_len": 250,
        "angle": 48.994995,
        "image_id": 7,
        "state": 1,
        "star_number": 143,
        "duration_ms": 812,
    },
    "code": 0,
    "id": 13770,
}

_ACCEPTED_START_SOLVE = {
    "jsonrpc": "2.0",
    "method": "start_solve",
    "result": 0,
    "code": 0,
}


def _polling_solve_ctl(tmp_path, start_reply, solve_replies):
    """Like _solve_ctl, but get_solve_result answers a scripted sequence."""
    ctrl = _native_ctl(tmp_path, None)
    remaining = list(solve_replies)

    def side_effect(method, *_args, **_kwargs):
        if method == "start_solve":
            return start_reply
        assert method == "get_solve_result"
        return remaining.pop(0)

    ctrl.alpaca.method_sync.side_effect = side_effect
    return ctrl


#: The real asyncio.sleep, captured at import time before any test patches
#: server_mod.asyncio.sleep -- server_mod.asyncio IS the asyncio module (not a
#: copy), so patching its "sleep" attribute patches asyncio.sleep everywhere,
#: including this file's own `import asyncio`. fake_sleep below needs a
#: not-patched reference to yield to the event loop for real (see its
#: docstring).
_REAL_ASYNCIO_SLEEP = asyncio.sleep


def _mock_sleep(monkeypatch):
    """Replace asyncio.sleep with a recorder so polling tests never really sleep.

    Each call still does a real, zero-duration ``await _REAL_ASYNCIO_SLEEP(0)``
    -- not a real delay, but a genuine event-loop yield. Without it, a
    poll loop whose every awaited call resolves synchronously (an AsyncMock,
    plus a fake sleep with no internal await) never actually hands control
    back to the event loop, so an `asyncio.wait_for` guard wrapped around it
    cannot fire its cancellation and the test hangs for real instead of
    failing (round-1 fix regression test needs this to be a true guard).
    """
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        await _REAL_ASYNCIO_SLEEP(0)

    monkeypatch.setattr(server_mod.asyncio, "sleep", fake_sleep)
    return slept


async def test_plate_solve_polls_past_code_215_then_returns_the_solved_result(
    tmp_path, monkeypatch
):
    ctrl = _polling_solve_ctl(
        tmp_path,
        _ACCEPTED_START_SOLVE,
        [
            dict(_SOLVE_215_REPLY),
            dict(_SOLVE_215_REPLY),
            dict(_SOLVE_215_REPLY),
            dict(_SOLVED_846_REPLY),
        ],
    )
    slept = _mock_sleep(monkeypatch)

    result = await ctrl.plate_solve()

    assert result["ok"] is True
    assert result["solve_result"] == _SOLVED_846_REPLY
    assert result["ra_deg"] == pytest.approx(5.572092 * 15)
    assert result["dec_deg"] == pytest.approx(22.068264)
    assert result["angle_deg"] == pytest.approx(48.994995)
    assert result["fov_deg"] == [0.712755, 1.269035]
    assert result["star_number"] == 143
    assert result["solve_duration_ms"] == 812
    assert result["waited_s"] == pytest.approx(6.0)
    assert slept == [2.0, 2.0, 2.0], "default poll interval is ~2s"
    methods = [c.args[0] for c in ctrl.alpaca.method_sync.await_args_list]
    assert methods == ["start_solve"] + ["get_solve_result"] * 4


async def test_plate_solve_gives_up_after_code_215_past_the_timeout(
    tmp_path, monkeypatch
):
    ctrl = _polling_solve_ctl(
        tmp_path,
        _ACCEPTED_START_SOLVE,
        [dict(_SOLVE_215_REPLY) for _ in range(10)],  # more than the timeout needs
    )
    slept = _mock_sleep(monkeypatch)

    result = await ctrl.plate_solve(poll_interval_s=1.0, timeout_s=3.0)

    assert result["ok"] is False
    assert "timeout" in result["error"].lower() or "timed out" in result["error"].lower()
    assert "215" in result["error"], "the last native code must reach the caller"
    assert "3.0" in result["error"], "the timeout itself must reach the caller"
    assert result["waited_s"] == pytest.approx(3.0)
    assert result["last_code"] == 215
    assert "solve_result" not in result
    assert slept == [1.0, 1.0, 1.0]
    methods = [c.args[0] for c in ctrl.alpaca.method_sync.await_args_list]
    assert methods == ["start_solve"] + ["get_solve_result"] * 4


async def test_plate_solve_fails_immediately_on_a_non_215_get_solve_result_error(
    tmp_path, monkeypatch
):
    # A non-215 native error (e.g. 207) is a real failure, not "still solving" --
    # fail immediately, with no further polling.
    ctrl = _polling_solve_ctl(
        tmp_path, _ACCEPTED_START_SOLVE, [{"error": "fail to operate", "code": 207}]
    )
    slept = _mock_sleep(monkeypatch)

    result = await ctrl.plate_solve()

    assert result["ok"] is False
    assert "207" in result["error"]
    assert "solve_result" not in result
    assert slept == [], "a non-215 error must not trigger a poll wait"
    methods = [c.args[0] for c in ctrl.alpaca.method_sync.await_args_list]
    assert methods == ["start_solve", "get_solve_result"]


async def test_plate_solve_tool_description_states_wait_and_degrees():
    tools = {t.name: t for t in await mcp.list_tools()}
    desc = tools["plate_solve"].description or ""
    assert "30" in desc, "must state the call may take up to ~30s"
    assert "degrees" in desc.lower(), "must state RA is returned in degrees"


# --- plate_solve's poll loop must be bounded independently of poll_interval_s
# (fix round 1, review finding F1): waited_s only advanced by poll_interval_s
# per iteration, so poll_interval_s=0 against a device stuck at code 215 spun
# the loop forever -- 0 >= timeout_s never became true. Confirmed live:
# ctrl.plate_solve(poll_interval_s=0, timeout_s=1.0) against an always-215
# fake hung past a 3s wall-clock guard.


def _always_215_solve_ctl(tmp_path, start_reply):
    """Like _polling_solve_ctl, but get_solve_result NEVER stops answering 215.

    A queued list of replies would just run out and raise; a device that is
    truly stuck answers 215 indefinitely, which is exactly the case that must
    not spin the loop forever.
    """
    ctrl = _native_ctl(tmp_path, None)

    def side_effect(method, *_args, **_kwargs):
        if method == "start_solve":
            return start_reply
        assert method == "get_solve_result"
        return dict(_SOLVE_215_REPLY)

    ctrl.alpaca.method_sync.side_effect = side_effect
    return ctrl


async def test_plate_solve_zero_poll_interval_does_not_spin_forever(
    tmp_path, monkeypatch
):
    # asyncio.sleep is mocked (never really sleeps), so the only thing that
    # can catch a true infinite loop is the wall-clock wait_for guard below --
    # a regression here must fail the test, not hang the suite.
    ctrl = _always_215_solve_ctl(tmp_path, _ACCEPTED_START_SOLVE)
    slept = _mock_sleep(monkeypatch)

    result = await asyncio.wait_for(
        ctrl.plate_solve(poll_interval_s=0, timeout_s=1.0), timeout=3.0
    )

    assert result["ok"] is False
    assert "215" in result["error"]
    assert "solve_result" not in result
    methods = [c.args[0] for c in ctrl.alpaca.method_sync.await_args_list]
    # Bounded: nowhere near "forever". Loose bound to avoid float-rounding
    # brittleness on the waited_s accumulation.
    assert len(methods) <= 20, f"poll loop was not bounded: {len(methods)} calls"
    assert len(slept) <= 20


@pytest.mark.parametrize("timeout_s", [0.0, -5.0])
async def test_plate_solve_non_positive_timeout_fails_immediately(
    tmp_path, monkeypatch, timeout_s
):
    ctrl = _polling_solve_ctl(
        tmp_path, _ACCEPTED_START_SOLVE, [dict(_SOLVE_215_REPLY)]
    )
    slept = _mock_sleep(monkeypatch)

    result = await ctrl.plate_solve(timeout_s=timeout_s)

    assert result["ok"] is False
    assert "215" in result["error"]
    assert "solve_result" not in result
    assert slept == [], "a non-positive timeout must not poll-wait at all"
    methods = [c.args[0] for c in ctrl.alpaca.method_sync.await_args_list]
    assert methods == ["start_solve", "get_solve_result"]


# --- get_status carries the authoritative native mount state -----------------
# Alpaca /tracking and /atpark disagree with the device on fw 7.75 and 8.46;
# get_device_state's mount.close (True = arm folded) is the authoritative park
# signal (CLAUDE.md). No tool exposed it, so the skills could not confirm a park.

#: A fw 8.46-shaped get_device_state reply: the JSON-RPC envelope, the validated
#: paths (device.is_verified, location_lon_lat, pi_status.battery_capacity) and
#: the mount block with close/tracking. Other device keys trimmed.
#:
#: The mount block itself is captured verbatim (live test 2026-09-24), PARKED:
#: {"move_type": "none", "close": True, "tracking": False, "equ_mode": False}
#: -- replacing an earlier, merely-documented shape that happened to omit
#: equ_mode. `close` became True after park; while imaging it read False (see
#: DEVICE_STATE_846_IMAGING below) -- both captured on the same run.
DEVICE_STATE_846 = {
    "jsonrpc": "2.0",
    "Timestamp": "412.118804211",
    "method": "get_device_state",
    "result": {
        "device": {"name": "Seestar S50", "is_verified": True},
        "setting": {"lang": "en"},
        "location_lon_lat": [-75.7, 45.4],
        "pi_status": {"battery_capacity": 87},
        "mount": {
            "move_type": "none",
            "close": True,
            "tracking": False,
            "equ_mode": False,
        },
    },
    "code": 0,
    "id": 90311,
}

#: Same capture, mid-session while imaging: the arm is unfolded (`close`
#: False). `tracking` still reads False from the device even while actively
#: on-target -- Alpaca's own `/tracking` disagrees with this on the same
#: hardware (see CLAUDE.md), which is exactly why get_status treats this
#: native field, not Alpaca's, as authoritative.
DEVICE_STATE_846_IMAGING = {
    **DEVICE_STATE_846,
    "result": {
        **DEVICE_STATE_846["result"],
        "mount": {
            "move_type": "none",
            "close": False,
            "tracking": False,
            "equ_mode": False,
        },
    },
}


def test_parse_mount_state_reads_the_fw846_nested_shape():
    from seestar_mcp.server import _parse_mount_state

    assert _parse_mount_state(DEVICE_STATE_846) == (True, False)  # parked
    assert _parse_mount_state(DEVICE_STATE_846_IMAGING) == (False, False)  # imaging
    # Flat mount dict for simple mocks.
    assert _parse_mount_state({"mount": {"close": True, "tracking": True}}) == (
        True,
        True,
    )
    # One field missing -> only that field is unknown.
    assert _parse_mount_state({"result": {"mount": {"close": True}}}) == (True, None)


def test_parse_mount_state_is_unknown_on_junk():
    from seestar_mcp.server import _parse_mount_state

    for junk in (
        None,
        {},
        "Error: Exceeded allotted wait time for result",
        [True, False],
        MagicMock(),
        NATIVE_ERROR_REPLY,
        {"result": {}},
        {"result": {"mount": None}},
        {"result": {"mount": "folded"}},
        {"result": "Error: x"},
        {"focus_pos": 1500, "tracking": True},  # no mount block at all
    ):
        assert _parse_mount_state(junk) == (None, None), junk
    # Non-bool values are not guessed at: a misread park signal is worse than none.
    odd = {"result": {"mount": {"close": 1, "tracking": "no"}}}
    assert _parse_mount_state(odd) == (None, None)


def _status_ctl(tmp_path, device_reply=None, device_exc=None):
    from seestar_mcp.config import Settings

    alpaca = AsyncMock()
    alpaca.get_connected.return_value = True
    alpaca.get_ra.return_value = 1.7
    alpaca.get_dec.return_value = 51.5
    alpaca.get_tracking.return_value = True  # Alpaca's (disagreeing) view
    alpaca.is_slewing.return_value = False
    if device_exc is not None:
        alpaca.method_sync.side_effect = device_exc
    else:
        alpaca.method_sync.return_value = device_reply
    return SeestarController(
        settings=Settings(_env_file=None, data_dir=tmp_path),
        provenance=MagicMock(),
        alpaca=alpaca,
        data=AsyncMock(),
        tier1=AsyncMock(),
    )


_ALPACA_STATUS = {
    "ok": True,
    "connected": True,
    "rightascension": 1.7,
    "declination": 51.5,
    "tracking": True,
    "slewing": False,
}


async def test_get_status_carries_the_native_mount_state(tmp_path):
    ctrl = _status_ctl(tmp_path, device_reply=DEVICE_STATE_846)
    out = await ctrl.get_status()
    # Existing keys and values unchanged; the native fields are additive.
    assert {k: out[k] for k in _ALPACA_STATUS} == _ALPACA_STATUS
    assert out["mount_parked"] is True
    assert out["mount_tracking"] is False
    assert ctrl.alpaca.method_sync.await_count == 1
    assert ctrl.alpaca.method_sync.await_args.args == ("get_device_state",)


async def test_get_status_carries_the_native_mount_state_while_imaging(tmp_path):
    # Captured live 2026-09-24: mid-session the mount reports close:False
    # (arm unfolded) and tracking:False (Alpaca disagrees; see CLAUDE.md).
    ctrl = _status_ctl(tmp_path, device_reply=DEVICE_STATE_846_IMAGING)
    out = await ctrl.get_status()
    assert {k: out[k] for k in _ALPACA_STATUS} == _ALPACA_STATUS
    assert out["mount_parked"] is False
    assert out["mount_tracking"] is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"device_reply": dict(NATIVE_ERROR_REPLY)},
        {"device_reply": "Error: Exceeded allotted wait time for result"},
        {"device_reply": None},
        {"device_exc": AlpacaError(1025, "ValueNotSet", "action")},
        {"device_exc": RuntimeError("bridge fell over")},
    ],
    ids=["error-dict", "error-string", "none", "alpaca-error", "any-exception"],
)
async def test_get_status_native_read_failure_is_unknown_not_fatal(tmp_path, kwargs):
    ctrl = _status_ctl(tmp_path, **kwargs)
    out = await ctrl.get_status()
    assert {k: out[k] for k in _ALPACA_STATUS} == _ALPACA_STATUS
    assert out["mount_parked"] is None
    assert out["mount_tracking"] is None


async def test_get_status_description_names_the_authoritative_fields():
    tools = {t.name: t for t in await mcp.list_tools()}
    desc = tools["get_status"].description or ""
    assert "mount_parked" in desc and "mount_tracking" in desc
    assert "get_device_state" in desc
    assert "disagree" in desc.lower(), "must warn that Alpaca tracking is unreliable"
