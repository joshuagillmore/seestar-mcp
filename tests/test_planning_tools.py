"""Tests for the 5 MCP planning tools (site / conditions / observability / plan).

These drive the :class:`SeestarController` directly and mock the astropy /
weather engine so the tool layer is exercised deterministically and offline.
The registration test confirms all 5 tools are exposed (bringing the server to
23 tools total).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import seestar_mcp.server as server_mod
from seestar_mcp.config import Settings
from seestar_mcp.planning.astro import Observability
from seestar_mcp.planning.catalog import DsoTarget
from seestar_mcp.planning.ranker import TargetPlan
from seestar_mcp.planning.weather import ConditionsAssessment
from seestar_mcp.server import SeestarController, mcp

PLANNING_TOOLS = {
    "get_site_profile",
    "set_site_profile",
    "assess_conditions",
    "get_target_observability",
    "plan_targets",
}

PROJECT_TOOLS = {
    "list_projects",
    "get_project",
    "set_project_goal",
    "log_session_result",
    "recommend_projects",
}


def _controller(tmp_path) -> SeestarController:
    """A controller whose site profile persists under ``tmp_path``."""
    return SeestarController(
        settings=Settings(_env_file=None, data_dir=tmp_path),
        provenance=MagicMock(),
        alpaca=AsyncMock(),
        data=AsyncMock(),
        tier1=AsyncMock(),
    )


async def _tool_names() -> set[str]:
    return {t.name for t in await mcp.list_tools()}


def test_planning_tools_registered():
    names = asyncio.run(_tool_names())
    assert PLANNING_TOOLS <= names
    assert len(asyncio.run(mcp.list_tools())) == 34


def test_project_tools_registered():
    names = asyncio.run(_tool_names())
    assert PROJECT_TOOLS <= names
    assert len(asyncio.run(mcp.list_tools())) == 34


def test_goal_then_log_then_get(tmp_path):
    c = _controller(tmp_path)
    g = asyncio.run(c.set_project_goal("M31", 360))
    assert g["ok"] is True
    assert g["project"]["goal_minutes"] == 360

    logged = asyncio.run(c.log_session_result("M31", 25, 160, 150))
    assert logged["ok"] is True
    assert logged["project"]["collected_minutes"] == 25

    got = asyncio.run(c.get_project("M31"))
    assert got["ok"] is True
    assert got["project"]["collected_minutes"] == 25
    assert len(got["project"]["sessions"]) == 1


def test_log_session_result_backfills_median_fwhm(tmp_path):
    """median_fwhm must not stay null just because the caller omitted it.

    It is an optional argument, so in practice every record was written with
    None: the value only exists once subs are downloaded and scored at wind-down,
    and the operator rarely carries it back by hand. Source it from the newest QA
    report for the target instead.
    """
    import json as _json

    reports = tmp_path / "reports"
    reports.mkdir()
    # Two reports for the same target; the NEWEST must win (name sorts by time).
    (reports / "qa_report_m27-20260724T010000Z.json").write_text(
        _json.dumps({"target": "M27", "medians": {"fwhm": 9.9}}), encoding="utf-8"
    )
    (reports / "qa_report_m27-20260724T053152Z.json").write_text(
        _json.dumps({"target": "M27", "medians": {"fwhm": 4.31}}), encoding="utf-8"
    )
    # A different target's report must not be picked up.
    (reports / "qa_report_m2-20260724T060000Z.json").write_text(
        _json.dumps({"target": "M2", "medians": {"fwhm": 1.11}}), encoding="utf-8"
    )

    c = _controller(tmp_path)
    r = asyncio.run(c.log_session_result("M27", 35.2, 211, 211))
    assert r["ok"] is True
    assert r["project"]["sessions"][0]["median_fwhm"] == 4.31

    # An explicitly supplied value still wins over the backfill.
    r2 = asyncio.run(c.log_session_result("M27", 10.0, 60, 60, median_fwhm=3.0))
    assert r2["project"]["sessions"][-1]["median_fwhm"] == 3.0

    # No report for the target → stays None, no raise.
    r3 = asyncio.run(c.log_session_result("M99", 5.0, 30, 30))
    assert r3["ok"] is True
    assert r3["project"]["sessions"][0]["median_fwhm"] is None


def test_get_project_unknown(tmp_path):
    c = _controller(tmp_path)
    r = asyncio.run(c.get_project("M999"))
    assert r["ok"] is False
    assert "no project" in r["error"].lower()


def test_list_projects_empty_then_populated(tmp_path):
    c = _controller(tmp_path)
    empty = asyncio.run(c.list_projects())
    assert empty["ok"] is True
    assert empty["count"] == 0
    assert empty["projects"] == []

    asyncio.run(c.set_project_goal("M31", 360))
    populated = asyncio.run(c.list_projects())
    assert populated["ok"] is True
    assert populated["count"] == 1
    assert populated["projects"][0]["target_id"] == "M31"


def test_recommend_projects_tool(tmp_path):
    c = _controller(tmp_path)
    asyncio.run(c.set_project_goal("M31", 360))
    asyncio.run(c.log_session_result("M31", 60, 100, 90))
    recs = asyncio.run(c.recommend_projects())
    assert recs["ok"] is True
    assert recs["count"] == 1
    assert recs["projects"][0]["target_id"] == "M31"


def test_set_then_get_site_profile(tmp_path):
    c = _controller(tmp_path)
    r = asyncio.run(c.set_site_profile(name="Yard", lat=40.0, lon=-74.0, bortle=6))
    assert r["ok"] is True
    assert r["profile"]["bortle"] == 6

    g = asyncio.run(c.get_site_profile())
    assert g["ok"] is True
    assert g["profile"]["bortle"] == 6
    assert g["profile"]["name"] == "Yard"


def test_get_site_profile_none_when_unset(tmp_path):
    c = _controller(tmp_path)
    g = asyncio.run(c.get_site_profile())
    assert g["ok"] is False
    assert "site" in g["error"].lower()


def _canned_conditions() -> ConditionsAssessment:
    return ConditionsAssessment(
        go=True,
        suitability=88,
        cloud_cover_pct=5.0,
        dew_risk="low",
        wind_kph=6.0,
        transparency="good",
        seeing="good",
        moon_illum_frac=0.1,
        dark_window_utc=("2026-07-05T02:00:00Z", "2026-07-05T08:00:00Z"),
        source="open-meteo",
        reasons=["cloud cover 5%"],
    )


def _canned_plan() -> TargetPlan:
    target = DsoTarget(
        id="M27", name="Dumbbell Nebula", ra_deg=299.9, dec_deg=22.7,
        type="planetary_nebula", size_arcmin=8.0, magnitude=7.4,
    )
    obs = Observability(
        target_id="M27", max_alt_deg=72.7, transit_utc="2026-07-05T04:00:00Z",
        rise_utc=None, set_utc=None, dark_minutes_above_floor=180.0,
        dark_minutes_in_sweet_band=120.4, field_rotation_deg_per_hr_at_transit=10.0,
        usable_sub_minutes=40.0, transits_above_ceiling=True, moon_sep_deg=95.3,
        moon_alt_deg=20.0, moon_illum_frac=0.1,
        best_window_utc=("2026-07-05T03:00:00Z", "2026-07-05T05:00:00Z"),
    )
    return TargetPlan(
        target=target, score=91, reasons=["120 min clean sweet-band time"],
        best_window_utc=obs.best_window_utc, recommended_subs=722,
        recommended_exposure_s=10, framing_note="fits FOV (8')", observability=obs,
    )


def test_plan_targets_with_mocked_engine(tmp_path, monkeypatch):
    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="Yard", lat=40.0, lon=-74.0, bortle=6))["ok"]

    monkeypatch.setattr(server_mod, "dark_window", lambda site, when: ("a", "b"))
    monkeypatch.setattr(server_mod, "moon_illumination", lambda when: 0.1)
    monkeypatch.setattr(server_mod, "load_catalog", lambda: [])

    async def _fake_assess(site, window, illum, **kwargs):
        return _canned_conditions()

    monkeypatch.setattr(server_mod, "assess_conditions_weather", _fake_assess)
    monkeypatch.setattr(
        server_mod, "rank_targets",
        lambda *a, **k: [_canned_plan()],
    )

    r = asyncio.run(c.plan_targets())
    assert r["ok"] is True
    assert r["count"] == 1
    assert r["conditions"] == {"go": True, "suitability": 88, "source": "open-meteo"}
    t = r["targets"][0]
    assert t["id"] == "M27"
    assert t["type"] == "planetary_nebula"
    assert t["score"] == 91
    assert t["recommended_subs"] == 722
    assert t["max_alt_deg"] == 72.7
    assert t["moon_sep_deg"] == 95.3
    assert t["sweet_band_min"] == 120
    # Compact: no bulky nested observability dumped per target.
    assert "observability" not in t


def test_plan_targets_returns_dark_window_utc_and_threads_it_to_the_ranker(
    tmp_path, monkeypatch
):
    # Task 7 (2026-09-22 review): plan_targets already computes the window for
    # its weather assessment — it must surface that SAME window as a top-level
    # field (the rule "if a tool names a quantity, return it as a field") and
    # pass it to rank_targets instead of letting the ranker recompute it.
    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="Yard", lat=40.0, lon=-74.0, bortle=6))["ok"]

    monkeypatch.setattr(server_mod, "dark_window", lambda site, when: ("a", "b"))
    monkeypatch.setattr(server_mod, "moon_illumination", lambda when: 0.1)
    monkeypatch.setattr(server_mod, "load_catalog", lambda: [])

    async def _fake_assess(site, window, illum, **kwargs):
        return _canned_conditions()

    monkeypatch.setattr(server_mod, "assess_conditions_weather", _fake_assess)

    captured = {}

    def _fake_rank(*a, **k):
        captured.update(k)
        return [_canned_plan()]

    monkeypatch.setattr(server_mod, "rank_targets", _fake_rank)

    r = asyncio.run(c.plan_targets())
    assert r["ok"] is True
    assert r["dark_window_utc"] == ("a", "b")
    assert captured["dark_window_utc"] == ("a", "b")


def test_plan_targets_compact_and_project_aware(tmp_path, monkeypatch):
    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="Yard", lat=40.0, lon=-74.0, bortle=6))["ok"]
    # Seed a project so a non-empty store is loaded and passed through.
    asyncio.run(c.set_project_goal("M27", 360))

    monkeypatch.setattr(server_mod, "dark_window", lambda site, when: ("a", "b"))
    monkeypatch.setattr(server_mod, "moon_illumination", lambda when: 0.1)
    monkeypatch.setattr(server_mod, "load_catalog", lambda: [])

    async def _fake_assess(site, window, illum, **kwargs):
        return _canned_conditions()

    monkeypatch.setattr(server_mod, "assess_conditions_weather", _fake_assess)

    captured = {}

    def _fake_rank(*a, **k):
        captured.update(k)
        return [_canned_plan()]

    monkeypatch.setattr(server_mod, "rank_targets", _fake_rank)

    r = asyncio.run(c.plan_targets(prefer_projects=True, avoid_recent_days=3))
    assert r["ok"] is True
    assert r["count"] == 1
    t = r["targets"][0]
    assert t["id"] == "M27"
    assert "observability" not in t  # compact output unchanged
    # Project-aware: the loaded projects + clock were threaded into the ranker.
    assert captured["projects"] is not None
    assert "M27" in captured["projects"]
    assert captured["now_utc"] is not None
    assert captured["recent_days"] == 3


def test_plan_targets_no_projects_when_disabled(tmp_path, monkeypatch):
    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="Yard", lat=40.0, lon=-74.0, bortle=6))["ok"]

    monkeypatch.setattr(server_mod, "dark_window", lambda site, when: ("a", "b"))
    monkeypatch.setattr(server_mod, "moon_illumination", lambda when: 0.1)
    monkeypatch.setattr(server_mod, "load_catalog", lambda: [])

    async def _fake_assess(site, window, illum, **kwargs):
        return _canned_conditions()

    monkeypatch.setattr(server_mod, "assess_conditions_weather", _fake_assess)

    captured = {}

    def _fake_rank(*a, **k):
        captured.update(k)
        return []

    monkeypatch.setattr(server_mod, "rank_targets", _fake_rank)

    r = asyncio.run(c.plan_targets(prefer_projects=False))
    assert r["ok"] is True
    assert captured["projects"] is None


def test_unknown_target(tmp_path):
    c = _controller(tmp_path)
    asyncio.run(c.set_site_profile(name="Yard", lat=40.0, lon=-74.0))
    r = asyncio.run(c.get_target_observability("NotARealObject"))
    assert r["ok"] is False
    assert "unknown target" in r["error"].lower()


def test_get_target_observability_returns_dark_window_utc_and_threads_it(
    tmp_path, monkeypatch
):
    # Task 7 (2026-09-22 review): the tool must name the night it planned as a
    # field, and hand the window it computed straight to observability() rather
    # than letting observability() recompute it.
    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="Yard", lat=40.0, lon=-74.0, bortle=6))["ok"]

    monkeypatch.setattr(server_mod, "dark_window", lambda site, when: ("a", "b"))

    captured = {}

    def _fake_observability(site, target, when, dark_window_utc=None):
        captured["dark_window_utc"] = dark_window_utc
        return Observability(
            target_id=target.id, max_alt_deg=50.0, transit_utc="2026-07-05T04:00:00Z",
            rise_utc=None, set_utc=None, dark_minutes_above_floor=100.0,
            dark_minutes_in_sweet_band=80.0, field_rotation_deg_per_hr_at_transit=10.0,
            usable_sub_minutes=40.0, transits_above_ceiling=False, moon_sep_deg=90.0,
            moon_alt_deg=20.0, moon_illum_frac=0.1, best_window_utc=("a", "b"),
        )

    monkeypatch.setattr(server_mod, "observability", _fake_observability)

    r = asyncio.run(c.get_target_observability("M27"))
    assert r["ok"] is True
    assert r["dark_window_utc"] == ("a", "b")
    assert captured["dark_window_utc"] == ("a", "b")


# --- Autonomous-night tools (simulate_night / check_night_guardrails) -------

AUTONOMOUS_TOOLS = {"simulate_night", "check_night_guardrails"}

DARK = ("2026-07-05T02:00:00Z", "2026-07-05T08:00:00Z")


def test_autonomous_tools_registered():
    names = asyncio.run(_tool_names())
    assert AUTONOMOUS_TOOLS <= names
    assert len(asyncio.run(mcp.list_tools())) == 34


def test_simulate_night_issues_no_motion(tmp_path, monkeypatch):
    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="Yard", lat=40.0, lon=-74.0))["ok"]

    monkeypatch.setattr(server_mod, "dark_window", lambda site, when: DARK)

    async def _fake_plan_targets(date=None, types=None, limit=None):
        return {
            "ok": True,
            "conditions": {"go": True, "suitability": 88, "source": "open-meteo"},
            "targets": [
                {
                    "id": "B",
                    "name": "Beta",
                    "best_window_utc": ["2026-07-05T04:30:00Z", "2026-07-05T06:00:00Z"],
                    "recommended_subs": 300,
                },
                {
                    "id": "A",
                    "name": "Alpha",
                    "best_window_utc": ["2026-07-05T02:30:00Z", "2026-07-05T04:00:00Z"],
                    "recommended_subs": 300,
                },
            ],
        }

    monkeypatch.setattr(c, "plan_targets", _fake_plan_targets)

    r = asyncio.run(c.simulate_night())
    assert r["ok"] is True
    assert r["conditions"] == {"go": True, "suitability": 88, "source": "open-meteo"}
    assert r["dark_window_utc"] == list(DARK) or r["dark_window_utc"] == DARK
    # Ordered, non-overlapping schedule (A before B by window start).
    ids = [s["target_id"] for s in r["schedule"]]
    assert ids == ["A", "B"]
    assert r["schedule"][0]["end_utc"] <= r["schedule"][1]["start_utc"]
    assert r["projected_targets"] == 2

    # No motion whatsoever: the dry run touched no device command.
    c.alpaca.method_sync.assert_not_called()
    c.alpaca.put_property.assert_not_called()


def test_check_guardrails_disconnected_parks(tmp_path, monkeypatch):
    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="Yard", lat=40.0, lon=-74.0))["ok"]

    # Far-future dawn so the dawn-margin guardrail is not the trigger.
    monkeypatch.setattr(
        server_mod, "dark_window", lambda site, when: ("2026-07-05T02:00:00Z", "2099-01-01T00:00:00Z")
    )

    # Device state read fails -> _parse_device_health must yield connected=False.
    c.alpaca.method_sync.side_effect = RuntimeError("no get_device_state")

    async def _fake_assess(site, window, illum, **kwargs):
        return _canned_conditions()

    monkeypatch.setattr(server_mod, "assess_conditions_weather", _fake_assess)

    r = asyncio.run(c.check_night_guardrails(session_start_utc="2026-07-05T02:30:00Z"))
    assert r["ok"] is True
    assert r["action"] == "park_and_stop"
    assert r["proceed"] is False


def test_check_guardrails_no_site(tmp_path):
    c = _controller(tmp_path)
    r = asyncio.run(c.check_night_guardrails(session_start_utc="2026-07-05T02:30:00Z"))
    assert r["ok"] is False
    assert "site" in r["error"].lower()


# --- Location-aware horizon mask (GPS reconcile) ----------------------------

def _masked_engine(monkeypatch):
    """Stub the astronomy/weather engine and capture the site passed to the ranker."""
    monkeypatch.setattr(server_mod, "dark_window", lambda site, when: ("a", "b"))
    monkeypatch.setattr(server_mod, "moon_illumination", lambda when: 0.1)
    monkeypatch.setattr(server_mod, "load_catalog", lambda: [])

    async def _fake_assess(site, window, illum, **kwargs):
        return _canned_conditions()

    monkeypatch.setattr(server_mod, "assess_conditions_weather", _fake_assess)

    captured = {}

    def _fake_rank(*a, **k):
        captured["site"] = a[0]
        return [_canned_plan()]

    monkeypatch.setattr(server_mod, "rank_targets", _fake_rank)
    return captured


def _set_masked_site(c):
    assert asyncio.run(
        c.set_site_profile(
            name="Yard", lat=40.0, lon=-74.0, horizon_mask=[[45.0, 135.0, 30.0]]
        )
    )["ok"]


def test_plan_targets_location_within(tmp_path, monkeypatch):
    c = _controller(tmp_path)
    _set_masked_site(c)
    captured = _masked_engine(monkeypatch)
    monkeypatch.setattr(c, "_current_gps", AsyncMock(return_value=(40.0, -74.0)))

    r = asyncio.run(c.plan_targets())
    assert r["ok"] is True
    loc = r["location"]
    assert loc["matched"] is True
    assert loc["mask_applied"] is True
    assert loc["warning"] is None
    assert loc["site_name"] == "Yard"
    # Within tolerance: the real mask reaches the ranker.
    assert captured["site"].horizon_mask == [(45.0, 135.0, 30.0)]


def test_plan_targets_location_mismatch_discloses(tmp_path, monkeypatch):
    c = _controller(tmp_path)
    _set_masked_site(c)
    captured = _masked_engine(monkeypatch)
    monkeypatch.setattr(c, "_current_gps", AsyncMock(return_value=(10.0, 10.0)))

    r = asyncio.run(c.plan_targets())
    assert r["ok"] is True
    loc = r["location"]
    assert loc["matched"] is False
    assert loc["mask_applied"] is False
    assert isinstance(loc["warning"], str) and loc["warning"]
    # Mask stripped for the engine (blocked targets NOT dropped).
    assert captured["site"].horizon_mask == []
    # min_altitude_deg is preserved when the mask is stripped.
    assert captured["site"].min_altitude_deg == 20.0


def test_parse_gps_validated_firmware_schema():
    # HARDWARE-VALIDATED (fw 7.75): result.location_lon_lat is [lon, lat].
    # Coordinates below are synthetic — the test pins the [lon, lat] ORDER, not a place.
    dev = {"result": {"location_lon_lat": [-12.3456, 65.4321], "device": {}}}
    assert server_mod._parse_gps(dev) == (65.4321, -12.3456)
    # Fallback shapes still parse; junk fails safe to None.
    assert server_mod._parse_gps({"result": {"setting": {"lat": 10.0, "lon": 20.0}}}) == (10.0, 20.0)
    assert server_mod._parse_gps({"result": {"location_lon_lat": [None, 45.0]}}) is None
    assert server_mod._parse_gps({"result": {"location_lon_lat": [1.0]}}) is None
    assert server_mod._parse_gps({}) is None


def test_location_block_gps_unavailable(tmp_path, monkeypatch):
    c = _controller(tmp_path)
    _set_masked_site(c)
    _masked_engine(monkeypatch)
    monkeypatch.setattr(c, "_current_gps", AsyncMock(return_value=None))

    r = asyncio.run(c.plan_targets())
    assert r["ok"] is True
    loc = r["location"]
    assert loc["matched"] is None
    assert loc["mask_applied"] is True
    assert loc["distance_km"] is None
    assert "unverified" in loc["warning"].lower()


def test_assess_conditions_surfaces_location(tmp_path, monkeypatch):
    c = _controller(tmp_path)
    _set_masked_site(c)
    _masked_engine(monkeypatch)
    monkeypatch.setattr(c, "_current_gps", AsyncMock(return_value=(10.0, 10.0)))

    r = asyncio.run(c.assess_conditions())
    assert r["ok"] is True
    assert r["location"]["mask_applied"] is False


def test_simulate_night_surfaces_location(tmp_path, monkeypatch):
    c = _controller(tmp_path)
    _set_masked_site(c)
    monkeypatch.setattr(server_mod, "dark_window", lambda site, when: DARK)

    async def _fake_plan_targets(date=None, types=None, limit=None):
        return {
            "ok": True,
            "conditions": {"go": True, "suitability": 88, "source": "open-meteo"},
            "location": {
                "matched": False,
                "distance_km": 42.0,
                "site_name": "Yard",
                "mask_applied": False,
                "warning": "moved",
            },
            "targets": [],
        }

    monkeypatch.setattr(c, "plan_targets", _fake_plan_targets)

    r = asyncio.run(c.simulate_night())
    assert r["ok"] is True
    assert r["location"]["mask_applied"] is False


# --- Learned-obstruction tools (log / suggest / add horizon mask) ------------

OBSTRUCTION_TOOLS = {"log_sky_result", "suggest_horizon_mask", "add_horizon_mask"}


def test_obstruction_tools_registered():
    names = asyncio.run(_tool_names())
    assert OBSTRUCTION_TOOLS <= names
    assert len(asyncio.run(mcp.list_tools())) == 34


def test_add_horizon_mask_appends(tmp_path):
    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="Yard", lat=40.0, lon=-74.0))["ok"]

    r = asyncio.run(c.add_horizon_mask(45, 135, 25))
    assert r["ok"] is True

    g = asyncio.run(c.get_site_profile())
    masks = [list(m) for m in g["profile"]["horizon_mask"]]
    assert [45.0, 135.0, 25.0] in masks


def test_log_then_suggest(tmp_path):
    # log_sky_result can't vary the night (it stamps datetime.now); drive the
    # underlying record_sky_result directly with distinct nights, writing to the
    # controller's own sky-log path, then assert suggest_horizon_mask surfaces it.
    from seestar_mcp.planning.obstructions import record_sky_result

    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="Yard", lat=40.0, lon=-74.0))["ok"]
    p = c._sky_log_path()
    for night in ("2026-07-04", "2026-07-05", "2026-07-06", "2026-07-07"):
        record_sky_result(
            92.0, 22.0, ok=False, weather_ok=True,
            now_utc=f"{night}T04:00:00Z", lat=40.0, lon=-74.0, path=p,
        )
        record_sky_result(
            60.0, 22.0, ok=True, weather_ok=True,
            now_utc=f"{night}T04:10:00Z", lat=40.0, lon=-74.0, path=p,
        )

    r = asyncio.run(c.suggest_horizon_mask())
    assert r["ok"] is True
    assert r["count"] >= 1
    assert any(
        cand["az_min_deg"] <= 92 <= cand["az_max_deg"] and cand["alt_min_deg"] >= 20
        for cand in r["candidates"]
    )


def test_log_sky_result_needs_target_or_azalt(tmp_path):
    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="Yard", lat=40.0, lon=-74.0))["ok"]
    r = asyncio.run(c.log_sky_result())
    assert r["ok"] is False
    assert "target" in r["error"].lower() or "az" in r["error"].lower()


def test_log_sky_result_explicit_azalt_ok(tmp_path):
    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="Yard", lat=40.0, lon=-74.0))["ok"]
    r = asyncio.run(c.log_sky_result(az=90, alt=25, solved=True, weather_go=True))
    assert r["ok"] is True
    assert r["az"] == 90.0
    assert r["alt"] == 25.0
    assert r["solved"] is True
    assert r["weather_ok"] is True
    assert (tmp_path / "sky_failures.json").exists()


def test_suggest_horizon_mask_no_site(tmp_path):
    c = _controller(tmp_path)
    r = asyncio.run(c.suggest_horizon_mask())
    assert r["ok"] is False
    assert "site" in r["error"].lower()


def test_guardrails_read_battery_from_device_state_without_a_second_call(
    tmp_path, monkeypatch
):
    """Battery comes from the get_device_state we already made — no pi_get_info.

    HARDWARE-VERIFIED (fw 7.75): battery lives at
    ``result.pi_status.battery_capacity`` in ``get_device_state``. A 2026-07-12
    diagnosis correctly found it was not at the TOP level and wrongly concluded it
    was absent entirely, so the guardrail made a second native round-trip for a
    value it already held. Measured cost: 610 redundant device calls in one
    dashboard session, and one extra round-trip per guardrail check — which now
    runs every ~10 minutes INSIDE each slot, on the same link that starves under
    load.
    """
    c = _controller(tmp_path)
    asyncio.run(c.set_site_profile(name="Yard", lat=45.0, lon=-75.0, bortle=6))

    calls: list[str] = []

    async def _method_sync(method, params=None):
        calls.append(method)
        if method == "get_device_state":
            return {
                "result": {
                    "device": {"is_verified": True},
                    "pi_status": {"battery_capacity": 87, "charger_status": "Full"},
                }
            }
        raise AssertionError(f"unexpected extra device call: {method}")

    c.alpaca.method_sync = _method_sync

    # Stubbed explicitly (2026-09-22 review): unstubbed, this test reached the
    # live Open-Meteo API, and would have spent meteoblue credits had
    # SEESTAR_METEOBLUE_API_KEY been exported.
    weather_calls = []

    async def _fake_assess(site, window, illum, **kwargs):
        weather_calls.append(window)
        return _canned_conditions()

    monkeypatch.setattr(server_mod, "assess_conditions_weather", _fake_assess)
    out = asyncio.run(c.check_night_guardrails(session_start_utc="2026-08-02T02:00:00Z"))

    assert out["ok"] is True
    assert calls == ["get_device_state"], (
        f"exactly one device call expected; got {calls}"
    )
    assert len(weather_calls) == 1  # the stub, not the network, answered


# --- date semantics: a bare date is THAT evening's night (2026-09-22 review) ---
# `date or now` went straight into dark_window, whose nearest-night semantics
# plan LAST night from a morning call, and "2026-09-22" parsed to 00:00Z (20:00
# EDT on the 21st). The tools now resolve the instant via planning_when.

DC_LAT, DC_LON = 38.9, -77.0
SEP22_EVENING = ("2026-09-23T00:35", "2026-09-23T09:25")


def _near(actual_iso: str, expected_iso: str, tol_min: float = 5.0) -> bool:
    from datetime import datetime

    a = datetime.fromisoformat(actual_iso.replace("Z", "+00:00")).replace(tzinfo=None)
    return abs((a - datetime.fromisoformat(expected_iso)).total_seconds()) <= tol_min * 60


def _is_sep22_evening(window) -> bool:
    return _near(window[0], SEP22_EVENING[0]) and _near(window[1], SEP22_EVENING[1])


def _echo_weather(monkeypatch, captured: dict | None = None):
    """Real astronomy; the weather stub echoes the window it was asked about."""

    async def _fake_assess(site, window, illum, **kwargs):
        if captured is not None:
            captured["window"] = window
        a = _canned_conditions()
        a.dark_window_utc = window
        return a

    monkeypatch.setattr(server_mod, "assess_conditions_weather", _fake_assess)


def test_assess_conditions_bare_date_is_that_evenings_night(tmp_path, monkeypatch):
    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="DC", lat=DC_LAT, lon=DC_LON))["ok"]
    monkeypatch.setattr(c, "_current_gps", AsyncMock(return_value=None))
    _echo_weather(monkeypatch)

    r = asyncio.run(c.assess_conditions(date="2026-09-22"))
    assert r["ok"] is True
    assert _is_sep22_evening(r["dark_window_utc"]), r["dark_window_utc"]


def test_plan_targets_bare_date_ranks_that_evenings_night(tmp_path, monkeypatch):
    from seestar_mcp.planning.astro import dark_window
    from seestar_mcp.planning.site import SiteProfile

    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="DC", lat=DC_LAT, lon=DC_LON))["ok"]
    monkeypatch.setattr(c, "_current_gps", AsyncMock(return_value=None))
    monkeypatch.setattr(server_mod, "load_catalog", lambda: [])
    weather: dict = {}
    _echo_weather(monkeypatch, weather)
    ranked: dict = {}

    def _fake_rank(*a, **k):
        ranked["when"] = a[1]
        ranked["now_utc"] = k["now_utc"]
        return []

    monkeypatch.setattr(server_mod, "rank_targets", _fake_rank)

    r = asyncio.run(c.plan_targets(date="2026-09-22"))
    assert r["ok"] is True
    # Weather and ranking agree on the night beginning the evening of Sep 22.
    assert _is_sep22_evening(weather["window"]), weather["window"]
    site = SiteProfile(name="DC", lat_deg=DC_LAT, lon_deg=DC_LON)
    assert _is_sep22_evening(dark_window(site, ranked["when"])), ranked["when"]
    assert ranked["now_utc"] == ranked["when"]


def test_get_target_observability_bare_date_is_that_evenings_night(tmp_path):
    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="DC", lat=DC_LAT, lon=DC_LON))["ok"]

    r = asyncio.run(c.get_target_observability("M31", date="2026-09-22"))
    assert r["ok"] is True
    transit = r["observability"]["transit_utc"]
    assert SEP22_EVENING[0] <= transit[:16] <= SEP22_EVENING[1], transit


def test_simulate_night_bare_date_schedules_that_evenings_night(tmp_path, monkeypatch):
    from seestar_mcp.planning.astro import dark_window
    from seestar_mcp.planning.site import SiteProfile

    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="DC", lat=DC_LAT, lon=DC_LON))["ok"]
    planned: dict = {}

    async def _fake_plan_targets(date=None, types=None, limit=None):
        planned["date"] = date
        return {"ok": True, "conditions": None, "location": None, "targets": []}

    monkeypatch.setattr(c, "plan_targets", _fake_plan_targets)

    r = asyncio.run(c.simulate_night(date="2026-09-22"))
    assert r["ok"] is True
    assert _is_sep22_evening(r["dark_window_utc"]), r["dark_window_utc"]
    # plan_targets was handed the resolved instant, so it ranks the SAME night.
    site = SiteProfile(name="DC", lat_deg=DC_LAT, lon_deg=DC_LON)
    assert dark_window(site, planned["date"]) == tuple(r["dark_window_utc"])


# --- `now` block: live position vs floor/ceiling (live test 2026-09-24, Task 4) --
# Every heartbeat needed a scratch astropy script to get the current target's
# altitude/azimuth against the floor and ceiling; the tool reported the whole
# night, not now. `now` is about the REAL current time, independent of `date`
# (which only picks which night `observability`/`dark_window_utc` describe).


def _freeze_now(monkeypatch, iso: str) -> None:
    """Pin `datetime.now(timezone.utc)` to `iso` for the tool-layer clock read.

    Patches `server_mod.datetime` (the module-level name `get_target_
    observability` reads, per its own comment) rather than the stdlib
    `datetime` module itself — astropy reads the real wall clock internally
    (e.g. IERS polar-motion refresh) via its own `datetime` reference, and a
    global patch broke that with a strict-type check unrelated to this test.
    """
    import datetime as datetime_module

    frozen = datetime_module.datetime.fromisoformat(iso)

    class _FrozenDatetime(datetime_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz else frozen

    monkeypatch.setattr(server_mod, "datetime", _FrozenDatetime)


def test_get_target_observability_now_matches_azalt_at_even_with_date(
    tmp_path, monkeypatch
):
    """`now` reports the REAL current time even when `date` picks a different
    night, and its alt/az match the pure `azalt_at` engine at that instant."""
    from seestar_mcp.planning.astro import azalt_at
    from seestar_mcp.planning.catalog import find_target
    from seestar_mcp.planning.site import SiteProfile

    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="DC", lat=DC_LAT, lon=DC_LON))["ok"]

    frozen_iso = "2026-09-23T05:00:00+00:00"
    _freeze_now(monkeypatch, frozen_iso)

    # `date` picks a DIFFERENT night (Sep 22 evening) than the frozen "now".
    r = asyncio.run(c.get_target_observability("M31", date="2026-09-22"))
    assert r["ok"] is True
    assert "now" in r

    site = SiteProfile(name="DC", lat_deg=DC_LAT, lon_deg=DC_LON)
    expect_az, expect_alt = azalt_at(site, find_target("M31"), frozen_iso)

    now = r["now"]
    assert now["utc"] == frozen_iso
    assert now["az_deg"] == pytest.approx(expect_az)
    assert now["alt_deg"] == pytest.approx(expect_alt)


def test_get_target_observability_now_booleans_flip_across_floor_and_ceiling(
    tmp_path, monkeypatch
):
    """`now.above_floor`/`now.in_sweet_band` flip at the same floor/ceiling the
    rest of the tool uses, for the same fixed alt/az."""
    c = _controller(tmp_path)
    monkeypatch.setattr(server_mod, "azalt_at", lambda site, target, when: (180.0, 40.0))

    def _now_for(min_altitude_deg, field_rotation_ceiling_deg):
        assert asyncio.run(
            c.set_site_profile(
                name="DC",
                lat=DC_LAT,
                lon=DC_LON,
                min_altitude_deg=min_altitude_deg,
                field_rotation_ceiling_deg=field_rotation_ceiling_deg,
            )
        )["ok"]
        r = asyncio.run(c.get_target_observability("M31"))
        assert r["ok"] is True
        return r["now"]

    # alt=40 is below a 50-deg floor: below the floor, so out of the band too.
    below_floor = _now_for(50.0, 80.0)
    assert below_floor["above_floor"] is False
    assert below_floor["in_sweet_band"] is False

    # alt=40 sits inside [20, 60]: both true.
    in_band = _now_for(20.0, 60.0)
    assert in_band["above_floor"] is True
    assert in_band["in_sweet_band"] is True

    # alt=40 clears a 20-deg floor but exceeds a 30-deg ceiling: up, but too
    # high for clean alt-az subs.
    above_ceiling = _now_for(20.0, 30.0)
    assert above_ceiling["above_floor"] is True
    assert above_ceiling["in_sweet_band"] is False


def test_get_target_observability_now_honors_horizon_mask(tmp_path, monkeypatch):
    """A masked az/alt zeroes out both booleans, exactly like the whole-night
    `observability()` computation already does via `is_blocked`."""
    c = _controller(tmp_path)
    monkeypatch.setattr(server_mod, "azalt_at", lambda site, target, when: (180.0, 40.0))
    assert asyncio.run(
        c.set_site_profile(
            name="DC",
            lat=DC_LAT,
            lon=DC_LON,
            min_altitude_deg=20.0,
            field_rotation_ceiling_deg=60.0,
            horizon_mask=[[170.0, 190.0, 45.0]],
        )
    )["ok"]

    r = asyncio.run(c.get_target_observability("M31"))
    assert r["ok"] is True
    now = r["now"]
    # alt=40 clears the 20-deg global floor and sits under the 60-deg ceiling,
    # but az=180 falls in the masked 170-190 arc below its 45-deg amin.
    assert now["above_floor"] is False
    assert now["in_sweet_band"] is False


def test_get_target_observability_omitted_date_after_midnight_reports_in_progress_night(
    tmp_path, monkeypatch
):
    """An omitted `date` at ~01:00 local, after local midnight but still dark,
    reports the IN-PROGRESS night's `dark_window_utc` (dashboard session,
    2026-09-24 review) -- the same instant pinned at the engine layer in
    test_planning_astro.py::test_planning_when_after_local_midnight_still_in_the_dark_is_unchanged.
    """
    c = _controller(tmp_path)
    assert asyncio.run(c.set_site_profile(name="DC", lat=DC_LAT, lon=DC_LON))["ok"]

    _freeze_now(monkeypatch, "2026-09-23T05:00:00+00:00")  # ~01:00 EDT

    r = asyncio.run(c.get_target_observability("M31"))
    assert r["ok"] is True
    assert _is_sep22_evening(r["dark_window_utc"]), r["dark_window_utc"]
