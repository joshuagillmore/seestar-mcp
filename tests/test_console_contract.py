"""Contract tests for the SeeStar Console dashboard.

Generated from the field-dependency list the Console team supplied on
2026-08-01, which they derived from their own ``web/src/api/schemas.ts``.

**Why these exist.** Their client treats a parse failure as *the tool having
failed*, not as a degraded panel — and on the Live screen that renders as "the
scope is idle". They shipped that exact bug twice. So a change that drops a
required field does not thin out a UI; it takes out a screen and lies about the
telescope. These tests move that failure from their runtime to our build.

**Required vs nullable is the load-bearing distinction:**

* ``REQUIRED`` — the key must be present. Removing one breaks a screen.
* ``NULLABLE`` — the key must be present but may be ``None``. Removing one
  degrades a panel.

Anything not listed is free to change. This file encodes the consumer's needs,
not our preferences; when it conflicts with a refactor, the refactor is a
breaking change and needs a contract version bump plus a note to them.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import respx

from seestar_mcp.config import Settings
from seestar_mcp.server import SeestarController

FIXTURE_DIR = Path(__file__).parent / "fixtures"

#: Naive ISO-8601, no offset — the shape the planning layer emits.
#: The Console team had a helper that returned NaN for the offset-bearing form,
#: so a silent change here breaks date rendering rather than failing loudly.
NAIVE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$")
#: Offset-bearing ISO-8601 — the shape the provenance layer emits.
OFFSET_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+\d{2}:\d{2}$")


def _controller(tmp_path) -> SeestarController:
    return SeestarController(
        settings=Settings(_env_file=None, data_dir=tmp_path),
        provenance=MagicMock(),
        alpaca=AsyncMock(),
        data=AsyncMock(),
        tier1=AsyncMock(),
    )


def _require(payload: dict, keys: list[str], where: str) -> None:
    """Every key present (value may be anything, including None if nullable)."""
    missing = [k for k in keys if k not in payload]
    assert not missing, f"{where}: missing required key(s) {missing}"


# --- get_view_state --------------------------------------------------------
# Their most-burned surface. Three pins, all from real breakage.


def test_get_view_state_accepts_an_idle_scope_with_empty_result(tmp_path):
    """``result: {}`` with no ``View`` key must stay valid.

    This is the most common real response — a connected, idle scope — and the
    Console team now treats ``View`` as optional-and-nullable rather than merely
    nullable because of it. If we ever start synthesising a ``View`` here, or
    start erroring on the empty shape, their idle path breaks.
    """
    c = _controller(tmp_path)
    c.alpaca.method_sync.return_value = {"jsonrpc": "2.0", "result": {}, "code": 0}

    out = asyncio.run(c.get_view_state())

    _require(out, ["ok", "view_state"], "get_view_state")
    assert out["ok"] is True
    assert out["view_state"]["result"] == {}


def test_get_view_state_preserves_the_annotation_nesting(tmp_path):
    """Plate-solve output stays at ``Stack.Annotate.result.annotations[]``.

    Their hand-authored fixture once had ``pixelx``/``pixely``/``radius`` flat on
    ``Annotate``, and agreed with a schema that was wrong the same way, so the
    overlay silently mis-rendered. The nesting is the contract.
    """
    c = _controller(tmp_path)
    c.alpaca.method_sync.return_value = {
        "result": {
            "View": {
                "stage": "Stack",
                "target_name": "M76",
                "lp_filter": True,
                "Stack": {
                    "stacked_frame": 614,
                    "dropped_frame": 0,
                    "can_annotate": True,
                    "Annotate": {
                        "state": "complete",
                        "result": {
                            "image_size": [1080, 1920],
                            "annotations": [
                                {
                                    "type": "ngc",
                                    "names": ["M 76"],
                                    "pixelx": 309.1,
                                    "pixely": 1190.6,
                                    "radius": 315.8,
                                }
                            ],
                        },
                    },
                },
            }
        },
        "code": 0,
    }

    view = asyncio.run(c.get_view_state())["view_state"]["result"]["View"]

    # NOTE: fields on View are deliberately NOT _require()d. A real mid-acquisition
    # payload carries `Initialise` and `stage` but NO `Stack` key at all (observed
    # on hardware 2026-07-31 during a 3PPA alignment), and the Console schema has
    # them nullish for the same reason. Pinning presence here would fail on a
    # legitimate payload, and the natural fix — deleting the pin — would take the
    # nesting assertions below with it. The NESTING is the contract; the presence
    # of any given field is not.
    _require(view["Stack"], ["stacked_frame", "dropped_frame"], "View.Stack")

    annotate = view["Stack"]["Annotate"]
    _require(annotate, ["result"], "Stack.Annotate")
    ann = annotate["result"]["annotations"][0]
    # Position lives INSIDE result.annotations[], not flat on Annotate — that is
    # the shape that bit them. Presence of individual keys is not pinned.
    assert {"pixelx", "pixely", "radius"} <= set(ann), (
        "plate-solve position must stay inside result.annotations[]"
    )
    # image_size is consumed as a two-element array to scale the overlay.
    assert len(annotate["result"]["image_size"]) == 2


# --- get_status ------------------------------------------------------------


def test_get_status_keys_are_present_even_when_unreadable(tmp_path):
    """Only ``ok`` is required, but the fields must be PRESENT (may be None).

    They read all five Alpaca fields; absence and null are different failures on
    their side. v1.2.0 added the native ``mount_parked`` / ``mount_tracking``
    pair under the same rule: always present, ``None`` when the native read
    failed. Here ``method_sync`` returns an unparseable mock, so both are None —
    exactly the case a consumer must still be able to parse.
    """
    c = _controller(tmp_path)
    c.alpaca.get_connected.return_value = True
    c.alpaca.get_ra.return_value = 1.7
    c.alpaca.get_dec.return_value = 51.5
    c.alpaca.get_tracking.return_value = True
    c.alpaca.is_slewing.return_value = False

    out = asyncio.run(c.get_status())
    _require(
        out,
        ["ok", "connected", "rightascension", "declination", "tracking", "slewing",
         "mount_parked", "mount_tracking"],
        "get_status",
    )


# --- qa_tier2 --------------------------------------------------------------


def test_qa_tier2_subs_carry_metrics_and_keep_unanalysable_rows(tmp_path):
    """Per-sub metrics stay; a sub that could not be analysed stays in the array.

    They render those as "unanalysed" rows and join on ``name``, so filtering
    them out silently shortens a chart rather than reporting a gap.

    VALIDATED 2026-08-02: these pins were written against the work order's prose
    before a consumer existed. The Console team has since written Tier2Schema and
    parsed a real 25-sub payload with it — first try, no schema changes — and ran
    these same assertions against the tool's own output: all keys present, 25/25
    sub names unique, kept == len(keep_list) == non-REJECT == 19. The loop is
    closed; these are no longer intent.
    """
    c = _controller(tmp_path)
    paths = [
        str(FIXTURE_DIR / "good.fits"),
        str(FIXTURE_DIR / "bad_ecc.fits"),
        str(FIXTURE_DIR / "does_not_exist.fits"),  # forces metrics.error
    ]
    out = asyncio.run(c.qa_tier2(paths=paths))

    _require(out, ["ok", "summary", "keep_list"], "qa_tier2")
    summary = out["summary"]
    _require(
        summary,
        ["target", "total", "kept", "wfwhm", "medians", "dominant_reject_cause", "subs"],
        "qa_tier2.summary",
    )
    # The unanalysable sub is still present, with error set.
    assert len(summary["subs"]) == 3, "unanalysable subs must not be filtered out"
    by_name = {s["name"]: s for s in summary["subs"]}
    assert len(by_name) == 3, "sub names must be unique — they are the join key"

    for sub in summary["subs"]:
        _require(sub, ["name", "verdict", "reasons", "metrics"], "qa_tier2.subs[]")
        _require(
            sub["metrics"],
            [
                "star_count", "fwhm", "hfr", "eccentricity",
                "snr", "background", "scattered_light", "error",
            ],
            "qa_tier2.subs[].metrics",
        )
    # Strict JSON: no NaN/Infinity tokens may reach a consumer.
    json.loads(json.dumps(out, allow_nan=False))


# --- list_projects ---------------------------------------------------------


def test_list_projects_carries_full_session_history(tmp_path):
    """``sessions[]`` and its fields are required — the history table is built from it.

    Their archive de-duplicates by observing night using ``date_utc``. If a
    ``detail="summary"`` mode is added it MUST omit the ``sessions`` key entirely
    rather than return an empty list: omission fails their parse loudly, an empty
    list silently empties the table, which is the worse failure.
    """
    c = _controller(tmp_path)
    asyncio.run(c.log_session_result("M76", 102.3, 614, 614, notes="clean"))

    # detail="full" is what carries sessions[]; summary omits the key by design.
    out = asyncio.run(c.list_projects(detail="full"))
    _require(out, ["ok", "projects", "count"], "list_projects")

    project = out["projects"][0]
    _require(
        project,
        [
            "target_id", "target_name", "goal_minutes", "collected_minutes",
            "status", "created_utc", "updated_utc", "sessions", "notes",
        ],
        "list_projects.projects[]",
    )
    session = project["sessions"][0]
    _require(
        session,
        ["date_utc", "integration_minutes", "subs_total", "subs_kept", "notes",
         "median_fwhm"],
        "list_projects.projects[].sessions[]",
    )


# --- timestamp shapes ------------------------------------------------------


def test_timestamp_shapes_are_pinned_per_layer(tmp_path):
    """Two shapes exist and both are load-bearing — do not unify them silently.

    The planning layer emits NAIVE ISO (no offset); the projects/provenance layer
    emits OFFSET-BEARING ISO. The Console team asked us to pin "the naive shape",
    but that is only half the story: pinning the wrong one per field would break
    them just as effectively. Each is asserted where it actually occurs.
    """
    from seestar_mcp.planning.astro import dark_window
    from seestar_mcp.planning.site import SiteProfile

    # Planning layer: NAIVE (this is what feeds dark_window_utc / best_window_utc).
    start, end = dark_window(
        SiteProfile(name="x", lat_deg=45.0, lon_deg=-75.0), "2026-08-01T04:00:00Z"
    )
    for value in (start, end):
        assert NAIVE_ISO.match(value), (
            f"planning timestamps are naive (no offset); got {value!r}"
        )

    # Projects layer: OFFSET-BEARING.
    c = _controller(tmp_path)
    asyncio.run(c.log_session_result("M76", 10.0, 60, 60))
    session = asyncio.run(c.list_projects(detail="full"))["projects"][0]["sessions"][0]
    assert OFFSET_ISO.match(session["date_utc"]), (
        f"projects timestamps are offset-bearing; got {session['date_utc']!r}"
    )


# --- get_target_observability: invariants, not presence ---------------------


def test_observability_minutes_are_invariant_not_merely_present():
    """Pin the QUANTITY, not the key — presence cannot see semantic drift.

    The Console turns these values into geometry: the sweet-band timeline is
    positioned by arithmetic on the dark window and the ``dark_minutes_*`` pair.
    A units change (minutes → seconds), a sign flip, or a reference-frame change
    yields a number that is present, numeric, plausible and WRONG, and they draw a
    picture that looks correct. There is no absent state to fall back on, so
    ``_require`` is structurally blind to exactly the failure that matters here.

    Two of the three invariants they proposed hold. The third referenced a
    ``dark_minutes_total`` field that does not exist; the real upper bound is the
    dark window itself, which is both available and a stronger check — it ties the
    integrated minutes to the interval they were integrated over.
    """
    from datetime import datetime

    from seestar_mcp.planning.astro import dark_window, observability
    from seestar_mcp.planning.catalog import find_target
    from seestar_mcp.planning.site import SiteProfile

    # M82, not M31. At lat 45 its declination (+69.7) puts its MINIMUM altitude at
    # ~24.7 deg — above the 20-deg floor — so it is circumpolar-above-floor and
    # `above_floor` saturates the whole dark window at every season. That lets the
    # bound be a two-sided EQUALITY, which also catches a minutes-to-hours
    # deflation that a one-sided `<=` would pass. M31 (dec +41.3, min alt -3.7)
    # saturates only seasonally — 0 minutes in April, 332 in January — so it is a
    # fragile fixture for this assertion.
    site = SiteProfile(name="x", lat_deg=45.0, lon_deg=-75.0)
    when = "2026-08-01T04:00:00Z"
    obs = observability(site, find_target("M82"), when)

    # 1. Minutes, non-negative.
    assert obs.dark_minutes_above_floor >= 0
    assert obs.dark_minutes_in_sweet_band >= 0

    # 2. Bounded by the dark window they are integrated over (replaces the
    #    proposed dark_minutes_total, which is not a field we emit).
    start, end = dark_window(site, when)
    window_min = (
        datetime.fromisoformat(end) - datetime.fromisoformat(start)
    ).total_seconds() / 60.0

    # Slack is one 2-min grid step PLUS an epsilon. A bare `+ 2.0` has zero margin
    # and does fire on legitimate values: measured excesses of +2.0069 (M31,
    # 2026-10-15) and +2.0098 (M82, 2026-04-15) come from counting inclusive grid
    # samples against a non-multiple-of-step window, not from any error.
    GRID_STEP_MIN = 2.0
    EPS = 0.1
    assert obs.dark_minutes_above_floor <= window_min + GRID_STEP_MIN + EPS, (
        "minutes above the floor cannot exceed the dark window"
    )
    # Two-sided: for a circumpolar-above-floor target this must SATURATE the
    # window. A one-sided bound would pass a minutes->hours deflation silently.
    assert abs(obs.dark_minutes_above_floor - window_min) <= GRID_STEP_MIN + EPS, (
        "M82 is above the floor all night at this latitude; above_floor must equal "
        "the dark window. A units deflation would show here and nowhere else."
    )

    # 3. The sweet band is a SUBSET of above-floor, so it can never exceed it.
    #    Structural, not incidental — astro.py builds `sweet` as `above_floor`
    #    with the ceiling condition ANDed on.
    assert obs.dark_minutes_in_sweet_band <= obs.dark_minutes_above_floor

    # A units change to seconds would break (2); a sign flip breaks (1); a
    # reference-frame change that lifts the target out of the window breaks (2).


def test_summary_omits_sessions_rather_than_emptying_it(tmp_path):
    """``detail="summary"`` must REMOVE the key, not return an empty list.

    An empty list parses cleanly and renders an empty history table — silently
    wrong, and indistinguishable from a project that genuinely has no sessions.
    Omission makes a consumer that needs the history fail loudly instead. This is
    the same argument that applies to ``targets_remaining`` in run_state.
    """
    c = _controller(tmp_path)
    asyncio.run(c.log_session_result("M76", 102.3, 614, 614, notes="clean"))

    summary = asyncio.run(c.list_projects())["projects"][0]
    assert "sessions" not in summary, "summary must omit the key, not empty it"
    assert summary["sessions_count"] == 1
    assert summary["last_session_utc"] is not None

    # detail="full" reproduces the historical payload exactly.
    full = asyncio.run(c.list_projects(detail="full"))["projects"][0]
    _require(full, ["sessions"], "list_projects(detail=full)")
    assert len(full["sessions"]) == 1


def test_last_session_utc_is_the_latest_date_not_the_last_element(tmp_path):
    """A backfilled session must not report an older date as "last".

    ``now_utc`` is caller-supplied (the determinism rule) and nothing sorts the
    list on load, so appending a session for an earlier night puts an older date
    in the final slot. ``sessions[-1]`` would report it as the most recent.
    """
    from seestar_mcp.planning.projects import log_session_result as _log

    p = tmp_path / "projects.json"
    _log("M76", "M76", integration_minutes=10, subs_total=1, subs_kept=1,
         now_utc="2026-08-01T04:00:00Z", path=p)
    # A session backfilled for an EARLIER night, appended afterwards.
    _log("M76", "M76", integration_minutes=10, subs_total=1, subs_kept=1,
         now_utc="2026-07-05T04:00:00Z", path=p)

    from seestar_mcp.planning.projects import load_projects
    from seestar_mcp.server import _project_payload

    project = load_projects(p)["M76"]
    summary = _project_payload(project, "summary")
    assert summary["sessions_count"] == 2
    assert summary["last_session_utc"] == "2026-08-01T04:00:00Z", (
        "last_session_utc must be the latest date, not the last list element"
    )


def test_recommend_projects_has_the_same_detail_escape_hatch(tmp_path):
    """Both project tools take ``detail`` — one escape hatch is not enough.

    If only ``list_projects`` gained the parameter, a consumer hitting the new
    summary default on ``recommend_projects`` would have no way to opt back out.
    """
    c = _controller(tmp_path)
    asyncio.run(c.log_session_result("M76", 10.0, 60, 60))

    summary = asyncio.run(c.recommend_projects())["projects"][0]
    assert "sessions" not in summary
    assert summary["sessions_count"] == 1

    full = asyncio.run(c.recommend_projects(detail="full"))["projects"][0]
    _require(full, ["sessions"], "recommend_projects(detail=full)")


def test_aggregates_are_rounded_like_the_per_sub_metrics(tmp_path):
    """medians and wfwhm round to the same precision as subs[].metrics.

    They are derived from the same measurements, so emitting 16 significant
    digits for the aggregate while rounding the values it summarises implied a
    precision difference that does not exist. Reported by the Console team, who
    were formatting them client-side rather than trusting the payload.
    """
    c = _controller(tmp_path)
    out = asyncio.run(
        c.qa_tier2(paths=[
            str(FIXTURE_DIR / "good.fits"),
            str(FIXTURE_DIR / "bad_ecc.fits"),
            str(FIXTURE_DIR / "hazy.fits"),
        ])
    )
    summary = out["summary"]

    assert summary["wfwhm"] == round(summary["wfwhm"], 4)
    for key, value in summary["medians"].items():
        if isinstance(value, float):
            assert value == round(value, 4), f"medians.{key} not rounded"


def test_report_ships_the_effective_thresholds_it_applied(tmp_path):
    """The cutoffs a verdict was made against must be FIELDS, not prose.

    Requested by the Console team, and it is the eleventh instance of the pattern
    their original work order named: if a tool states a quantity in prose, return
    it as a field too. The numbers existed only inside reasons[] —
    "REJECT: scattered light 0.016 > 0.014 (median + 2sigma)" — so a chart that
    wants to draw the cutoff line had to parse a sentence, which is re-deriving a
    verdict. They shipped without the lines rather than do that.

    These must be the EFFECTIVE per-session values, not the config constants:
    the SNR, star-count and scatter cutoffs derive from that session's own
    medians, so two nights reject at different absolute numbers and only the
    per-session value can position a line.
    """
    c = _controller(tmp_path)
    out = asyncio.run(
        c.qa_tier2(paths=[
            str(FIXTURE_DIR / "good.fits"),
            str(FIXTURE_DIR / "bad_ecc.fits"),
            str(FIXTURE_DIR / "hazy.fits"),
        ])
    )
    thresholds = out["summary"]["thresholds"]

    # The two lines the eccentricity chart asked for. The reject line is absolute
    # (0.575, the canonical PixInsight cutoff) and pinned as such. The marginal
    # line became session-derived on 2026-08-01 -- it is max(median + 1sigma,
    # 0.42) -- so pinning it to the config constant would contradict this test's
    # own rule above. Pin the invariants instead: present, a fraction, and never
    # below the perceptibility floor, which is what a chart actually relies on.
    assert thresholds["eccentricity_reject"] == 0.575
    ecc_marginal = thresholds["eccentricity_marginal"]
    assert isinstance(ecc_marginal, float)
    assert 0.42 <= ecc_marginal <= 1.0

    # Session-derived floors are present and are NOT the raw config factors.
    for key in ("snr_floor", "star_count_floor"):
        assert key in thresholds, f"{key} missing"
    medians = out["summary"]["medians"]
    if thresholds["snr_floor"] is not None and medians.get("snr"):
        # 0.5 x median by default — a value, not the factor.
        assert thresholds["snr_floor"] != 0.5
        assert abs(thresholds["snr_floor"] - medians["snr"] * 0.5) < 0.01

    # Rounded like every other reported float, and strict-JSON safe.
    for key, value in thresholds.items():
        if isinstance(value, float):
            assert value == round(value, 4), f"thresholds.{key} not rounded"
    json.loads(json.dumps(out, allow_nan=False))


# --- assess_conditions: units, because a pct->fraction change is silent -----


@respx.mock
async def test_assess_conditions_contract_and_units(tmp_path):
    """Keys, plus the UNIT invariants that presence checks cannot see.

    Ranked second by the Console team on "how silently a change would break us":
    ``cloud_cover_pct`` and ``wind_kph`` are rendered as numbers with units
    attached, so a percent-to-fraction change turns 40% cloud into "0.4%" and
    reads as a clear night. Nothing is absent, nothing fails to parse, and the
    verdict on screen is the opposite of the truth.
    """
    respx.get(url__startswith="https://api.open-meteo.com/v1/forecast").mock(
        return_value=httpx.Response(
            200,
            json={
                "hourly": {
                    "time": ["2026-08-02T02:00", "2026-08-02T03:00"],
                    "cloudcover_low": [10, 40],
                    "cloudcover_mid": [0, 5],
                    "cloudcover_high": [10, 10],
                    "relativehumidity_2m": [60, 62],
                    "dewpoint_2m": [8, 8],
                    "temperature_2m": [18, 17],
                    "windspeed_10m": [5, 6],
                    "precipitation_probability": [0, 10],
                }
            },
        )
    )
    c = _controller(tmp_path)
    await c.set_site_profile(name="Yard", lat=45.0, lon=-75.0, bortle=6)
    out = await c.assess_conditions()

    _require(
        out,
        ["ok", "location", "suitability", "dew_risk", "moon_illum_frac",
         "dark_window_utc", "source", "reasons", "go", "cloud_cover_pct",
         "wind_kph", "transparency", "seeing"],
        "assess_conditions",
    )
    _require(out["location"], ["site_name", "mask_applied"], "location")

    # UNITS — the invariants that catch what presence cannot.
    assert 0.0 <= out["cloud_cover_pct"] <= 100.0, "cloud cover is a PERCENT (0-100)"
    assert out["cloud_cover_pct"] > 1.0, (
        "a fraction would land in 0..1 and read as a clear night"
    )
    assert out["wind_kph"] >= 0.0
    assert 0.0 <= out["moon_illum_frac"] <= 1.0, "moon illumination is a FRACTION (0-1)"
    assert 0 <= out["suitability"] <= 100
    assert len(out["dark_window_utc"]) == 2, "consumed as a two-element tuple"
    # go is tri-state on their side: True / False / None all render distinctly.
    assert out["go"] in (True, False, None)


# --- qa_tier1 ---------------------------------------------------------------


def test_qa_tier1_contract(tmp_path):
    """``trends`` is their only optional here; everything else is required.

    Ranked third: drift is loud because a missing required field fails their
    parse immediately. Pinned so the loudness is guaranteed rather than assumed.
    """
    from seestar_mcp.qa_tier1 import Tier1Snapshot

    c = _controller(tmp_path)
    # poll() returns the dataclass; the controller asdict()s it and drops `raw`.
    c.tier1.poll = AsyncMock(
        return_value=Tier1Snapshot(
            ts="2026-08-02T04:00:00+00:00",
            stacked=42, rejected=1, solve_ok=True, solve_rms=None,
            focus_pos=1567, hfd=2.2, tracking=True,
            raw={"bulky": "kept in provenance, trimmed from the payload"},
        )
    )
    # check/status_line/trends are SYNC on Tier1Monitor — an AsyncMock here would
    # hand the payload three coroutines instead of values.
    c.tier1.check = MagicMock(return_value=[])
    c.tier1.status_line = MagicMock(
        return_value="stacked 42 | rejected 1 | solve OK"
    )
    c.tier1.trends = MagicMock(
        return_value={"stacked_delta": 5, "rejected_delta": 0,
                      "focus_delta": 0, "hfd_delta": None}
    )
    out = asyncio.run(c.qa_tier1())

    # `raw` is deliberately trimmed from the payload — they never see it.
    assert "raw" not in out["snapshot"]

    _require(out, ["ok", "snapshot", "flags", "status_line", "trends"], "qa_tier1")
    _require(out["snapshot"], ["ts"], "qa_tier1.snapshot")
    # They render status_line verbatim rather than composing one.
    assert isinstance(out["status_line"], str) and out["status_line"]


# --- plan_targets -----------------------------------------------------------


TARGET_TYPES = {
    "galaxy", "emission_nebula", "reflection_nebula", "planetary_nebula",
    "globular_cluster", "open_cluster", "supernova_remnant", "dark_nebula",
}


def test_plan_targets_contract_and_target_type_vocabulary(tmp_path, monkeypatch):
    """11 of 14 per-target fields are required; ``type`` drives display labels.

    A ``type`` value outside the agreed vocabulary is not a parse failure — it is
    a target that renders with no label, which is why the set is pinned here.
    """
    import seestar_mcp.server as server_mod

    c = _controller(tmp_path)
    asyncio.run(c.set_site_profile(name="Yard", lat=45.0, lon=-75.0, bortle=6))
    monkeypatch.setattr(server_mod, "dark_window", lambda site, when: ("a", "b"))
    monkeypatch.setattr(server_mod, "moon_illumination", lambda when: 0.1)
    monkeypatch.setattr(server_mod, "load_catalog", lambda: [])

    async def _fake_assess(site, window, illum, **kw):
        from seestar_mcp.planning.weather import ConditionsAssessment
        return ConditionsAssessment(
            go=True, suitability=88, cloud_cover_pct=5.0, dew_risk="low",
            wind_kph=6.0, transparency="good", seeing="good", moon_illum_frac=0.1,
            dark_window_utc=("a", "b"), source="open-meteo", reasons=["clear"],
        )

    monkeypatch.setattr(server_mod, "assess_conditions_weather", _fake_assess)
    out = asyncio.run(c.plan_targets())

    assert out["ok"] is True
    # dark_window_utc (v1.2.0) names the night that was planned.
    _require(
        out,
        ["ok", "location", "dark_window_utc", "conditions", "count", "targets"],
        "plan_targets",
    )
    for target in out["targets"]:
        _require(
            target,
            ["id", "name", "type", "score", "reasons", "best_window_utc",
             "recommended_subs", "recommended_exposure_s", "max_alt_deg",
             "sweet_band_min", "moon_sep_deg"],
            "plan_targets.targets[]",
        )
        assert target["type"] in TARGET_TYPES, (
            f"unknown target type {target['type']!r} renders with no label"
        )
        assert len(target["best_window_utc"]) == 2


# --- get_site_profile -------------------------------------------------------


def test_get_site_profile_contract(tmp_path):
    """Every field required — ranked LAST because a change fails their parse loudly.

    Pinned anyway for the two that are geometry: ``min_altitude_deg`` and
    ``field_rotation_ceiling_deg`` draw the sweet-band gauge's edges, so they must
    survive even if the gauge's live marker never arrives.
    """
    c = _controller(tmp_path)
    asyncio.run(c.set_site_profile(name="Yard", lat=45.0, lon=-75.0, bortle=6))
    out = asyncio.run(c.get_site_profile())

    _require(out, ["ok", "profile"], "get_site_profile")
    _require(
        out["profile"],
        ["name", "lat_deg", "lon_deg", "bortle", "min_altitude_deg",
         "field_rotation_ceiling_deg", "horizon_mask", "elevation_m"],
        "get_site_profile.profile",
    )
    profile = out["profile"]
    assert 0 < profile["min_altitude_deg"] < profile["field_rotation_ceiling_deg"] < 90, (
        "the gauge's edges must stay ordered and on-sky"
    )


def test_contract_version_is_declared_and_matches_this_suite():
    """docs/CONTRACT.md is the versioned artifact consumers pin against.

    It exists so the exchange of prose documents can stop: a consumer diffs a
    file instead of reading a work order. Pinned here so the version cannot drift
    out of the document silently, and so a reader of the tests can find it.
    """
    contract = (Path(__file__).parents[1] / "docs" / "CONTRACT.md").read_text(
        encoding="utf-8"
    )
    title = re.search(r"^# seestar-mcp consumer contract — v(\S+)", contract, re.M)
    assert title, "contract has no versioned title"
    version = title.group(1)
    assert version == "1.2.0"

    # The Status table is the field a consumer actually pins against, so it must
    # agree with the title. They drifted apart once (title said 1.0.1, the table
    # still said 1.0.0) and the consumer had already pinned from the title — the
    # same artifact-vs-its-own-description failure the version bump existed to fix.
    status = re.search(r"^\|\s*\*\*Version\*\*\s*\|\s*(\S+?)\s*\|", contract, re.M)
    assert status, "contract Status table has no Version row"
    assert status.group(1) == version, (
        f"Status table says {status.group(1)}, title says {version}"
    )
    # The rules a consumer relies on must actually be stated in it.
    for rule in (
        "Required means present, not truthy",
        "Unknown ≠ empty",
        "if a tool names a quantity in prose",
    ):
        assert rule.lower() in contract.lower(), f"contract is missing: {rule}"


# --- tool-level pins for the two the file was missing -----------------------


def test_get_run_state_tool_contract(tmp_path):
    """``run`` is not a liveness signal — pinned at the TOOL boundary.

    The behaviour is covered in ``test_run_state.py``, but a consumer pins the
    tool, not the module: the controller wrapper can drift independently. Both
    ``active`` and stale-``unknown`` carry a ``run`` record, so branching on its
    presence treats a dead session as live.
    """
    from seestar_mcp.run_state import RunState, write_run_state

    c = _controller(tmp_path)
    assert asyncio.run(c.get_run_state())["state"] == "idle"

    write_run_state(
        RunState(session_start_utc="2026-08-02T02:00:00+00:00", target="M76"),
        tmp_path / "run_state.json",
    )
    out = asyncio.run(c.get_run_state())
    _require(out, ["ok", "state", "run"], "get_run_state")
    assert out["state"] == "active"
    assert out["run"]["target"] == "M76"
    # Offset-bearing, as stated to consumers.
    assert OFFSET_ISO.match(out["stamped_utc"])


def test_get_target_observability_tool_contract(tmp_path, monkeypatch):
    """The TOOL wrapper's shape, not just the computation underneath it.

    The invariants test exercises ``observability()`` directly; this pins what a
    consumer actually receives, including the ``above_floor`` / ``in_sweet_band``
    pair whose *difference* is the only way to say a target is up but too high
    to use.
    """
    c = _controller(tmp_path)
    asyncio.run(c.set_site_profile(name="Yard", lat=45.0, lon=-75.0, bortle=6))
    out = asyncio.run(c.get_target_observability("M31"))

    assert out["ok"] is True
    # dark_window_utc (v1.2.0) names the night the observability describes;
    # now (live test 2026-09-24, Task 4) is the target's REAL live position,
    # present on every ok:true path regardless of dark_window_utc's night.
    _require(
        out,
        ["ok", "target", "observability", "dark_window_utc", "now"],
        "get_target_observability",
    )
    _require(
        out["now"],
        ["utc", "alt_deg", "az_deg", "above_floor", "in_sweet_band"],
        "get_target_observability.now",
    )
    if out.get("observability"):
        _require(
            out["observability"],
            ["target_id", "max_alt_deg", "dark_minutes_above_floor",
             "dark_minutes_in_sweet_band", "transits_above_ceiling",
             "moon_sep_deg", "best_window_utc"],
            "get_target_observability.observability",
        )
        obs = out["observability"]
        assert obs["dark_minutes_in_sweet_band"] <= obs["dark_minutes_above_floor"]


def test_eccentricity_cutoff_lines_never_cross(tmp_path):
    """v1.1.1: the two eccentricity lines a chart draws must stay ordered.

    The marginal line became session-derived in v1.1.0 and could exceed the
    absolute reject line on a poor session, which renders as a "marginal"
    cutoff drawn ABOVE the "reject" cutoff — a plausible-looking but incoherent
    chart. Both must also be finite: a NaN cutoff silently disabled the tier and
    would have made the strict-JSON renderer raise.
    """
    import math

    c = _controller(tmp_path)
    out = asyncio.run(
        c.qa_tier2(paths=[
            str(FIXTURE_DIR / "good.fits"),
            str(FIXTURE_DIR / "bad_ecc.fits"),
            str(FIXTURE_DIR / "hazy.fits"),
        ])
    )
    thresholds = out["summary"]["thresholds"]
    marginal = thresholds["eccentricity_marginal"]
    reject = thresholds["eccentricity_reject"]

    assert math.isfinite(marginal) and math.isfinite(reject)
    assert marginal <= reject, (
        f"marginal {marginal} is above reject {reject}: the tier is unreachable "
        "and a chart would draw its cutoff lines crossed"
    )


def test_contract_coverage_count_matches_the_tools_it_names():
    """The Status row's tool count must equal the Covered-tools list.

    This drifted twice: v1.0.1 fixed "9 while the list named 10", and it went
    stale again at 10-vs-11 because `list_projects` / `recommend_projects` share
    one line but are two tools. Counting the list here instead of trusting the
    prose means the next edit cannot reintroduce it.
    """
    contract = (Path(__file__).parents[1] / "docs" / "CONTRACT.md").read_text(
        encoding="utf-8"
    )
    covers = re.search(r"\*\*Covers\*\*\s*\|\s*(\d+) of (\d+) tools", contract)
    assert covers, "Status table has no 'Covers N of M tools' row"
    claimed, total = int(covers.group(1)), int(covers.group(2))

    section = re.search(
        r"## Covered tools\s*\n+(.*?)\n\nThat is", contract, re.S
    )
    assert section, "Covered tools section not found"
    listed = set(re.findall(r"`([a-z_0-9]+)`", section.group(1)))
    assert claimed == len(listed), (
        f"Status row claims {claimed} tools; the list names {len(listed)}: "
        f"{sorted(listed)}"
    )

    # And the denominator must be the real registry size.
    from seestar_mcp.server import mcp

    real = len(asyncio.run(mcp.list_tools()))
    assert total == real, f"contract says {total} total tools; server has {real}"

    # The "Enforced by" row states this file's own test count, and that drifted
    # too: it read 20 while the file held 21, because `async def test_` does not
    # match a `^def test_` count. Count both spellings rather than trust prose.
    own = re.findall(
        r"^(?:async )?def (test_\w+)", Path(__file__).read_text(encoding="utf-8"), re.M
    )
    enforced = re.search(r"\*\*Enforced by\*\*.*?\((\d+) tests", contract)
    assert enforced, "Status table has no 'Enforced by ... (N tests' row"
    assert int(enforced.group(1)) == len(own), (
        f"contract claims {enforced.group(1)} tests; this file defines {len(own)}"
    )
