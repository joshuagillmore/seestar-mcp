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
