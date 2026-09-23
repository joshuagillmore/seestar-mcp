"""Tests for the reasoned target ranker (Task 6).

The ranker's astronomy is injected via ``observability_fn`` so these tests are
fully deterministic and never touch astropy: each case hands the ranker a
hand-built :class:`Observability` record through the ``_obs`` helper.
"""

from __future__ import annotations

from seestar_mcp.planning.astro import Observability
from seestar_mcp.planning.catalog import DsoTarget, find_target
from seestar_mcp.planning.projects import Project, SessionRecord
from seestar_mcp.planning.ranker import TargetPlan, rank_targets
from seestar_mcp.planning.site import SiteProfile
from seestar_mcp.planning.weather import ConditionsAssessment


def _obs(minutes_band, above_ceiling=False, sep=90.0, usable_sub_minutes=40.0):
    return Observability(
        target_id="t",
        max_alt_deg=50.0,
        transit_utc="2026-07-05T04:00:00Z",
        rise_utc=None,
        set_utc=None,
        dark_minutes_above_floor=minutes_band + 30,
        dark_minutes_in_sweet_band=minutes_band,
        field_rotation_deg_per_hr_at_transit=10.0,
        usable_sub_minutes=usable_sub_minutes,
        transits_above_ceiling=above_ceiling,
        moon_sep_deg=sep,
        moon_alt_deg=20.0,
        moon_illum_frac=0.1,
        best_window_utc=("a", "b"),
    )


def _cond(go=True):
    return ConditionsAssessment(
        go=go,
        suitability=90 if go else 0,
        cloud_cover_pct=5,
        dew_risk="low",
        wind_kph=5,
        transparency="good",
        seeing="good",
        moon_illum_frac=0.1,
        dark_window_utc=("2026-07-05T02:00:00Z", "2026-07-05T08:00:00Z"),
        source="open-meteo",
        reasons=[],
    )


def test_more_sweet_band_time_ranks_higher():
    site = SiteProfile(name="x", lat_deg=40, lon_deg=-74, bortle=6)
    cat = [
        DsoTarget("A", "A", 0, 0, "emission_nebula", 20, 7),
        DsoTarget("B", "B", 0, 0, "emission_nebula", 20, 7),
    ]
    obs_map = {"A": _obs(120), "B": _obs(20)}
    plans = rank_targets(
        site,
        "2026-07-05T04:00:00Z",
        cat,
        _cond(),
        observability_fn=lambda s, t, w, dark_window_utc=None: obs_map[t.id],
    )
    assert [p.target.id for p in plans] == ["A", "B"]
    assert plans[0].reasons  # non-empty
    assert isinstance(plans[0], TargetPlan)


def test_never_up_target_excluded():
    site = SiteProfile(name="x", lat_deg=40, lon_deg=-74)
    cond = ConditionsAssessment(
        None, 0, None, "low", None, None, None, 0.1, ("a", "b"), "unknown", []
    )
    cat = [DsoTarget("Z", "Z", 0, -80, "galaxy", 10, 9)]
    plans = rank_targets(
        site,
        "2026-07-05T04:00:00Z",
        cat,
        cond,
        observability_fn=lambda s, t, w, dark_window_utc=None: _obs(0),  # zero sweet-band
    )
    assert plans == []  # dropped


def test_transits_above_ceiling_gets_field_rotation_reason():
    site = SiteProfile(name="x", lat_deg=40, lon_deg=-74, bortle=6)
    cat = [DsoTarget("A", "A", 0, 0, "emission_nebula", 20, 7)]
    plans = rank_targets(
        site,
        "2026-07-05T04:00:00Z",
        cat,
        _cond(),
        observability_fn=lambda s, t, w, dark_window_utc=None: _obs(90, above_ceiling=True),
    )
    assert len(plans) == 1
    joined = " ".join(plans[0].reasons).lower()
    assert "field rotation" in joined
    assert "ceiling" in joined


def test_sweet_band_target_has_no_sub_trail_reason():
    # A sweet-band target that never transits above the ceiling must NOT be
    # tagged "subs trail near transit" — that note is gated on near-zenith
    # transits (real hardware banks clean 10 s subs well below the ceiling).
    site = SiteProfile(name="x", lat_deg=40, lon_deg=-74, bortle=6)
    cat = [DsoTarget("A", "A", 0, 0, "emission_nebula", 20, 7)]
    plans = rank_targets(
        site,
        "2026-07-05T04:00:00Z",
        cat,
        _cond(),
        observability_fn=lambda s, t, w, dark_window_utc=None: _obs(
            90, above_ceiling=False, usable_sub_minutes=0.0
        ),
    )
    assert len(plans) == 1
    joined = " ".join(plans[0].reasons).lower()
    assert "subs trail" not in joined


def test_above_ceiling_target_gets_sub_trail_reason():
    # Near-zenith (above-ceiling) transit still gets both the field-rotation
    # reason and the sub-trail note.
    site = SiteProfile(name="x", lat_deg=40, lon_deg=-74, bortle=6)
    cat = [DsoTarget("A", "A", 0, 0, "emission_nebula", 20, 7)]
    plans = rank_targets(
        site,
        "2026-07-05T04:00:00Z",
        cat,
        _cond(),
        observability_fn=lambda s, t, w, dark_window_utc=None: _obs(90, above_ceiling=True),
    )
    assert len(plans) == 1
    joined = " ".join(plans[0].reasons).lower()
    assert "field rotation" in joined
    assert "subs trail" in joined


def test_recommended_subs_is_sweet_band_seconds_over_exposure():
    site = SiteProfile(name="x", lat_deg=40, lon_deg=-74, bortle=6)
    cat = [DsoTarget("A", "A", 0, 0, "emission_nebula", 20, 7)]
    plans = rank_targets(
        site,
        "2026-07-05T04:00:00Z",
        cat,
        _cond(),
        observability_fn=lambda s, t, w, dark_window_utc=None: _obs(120),
    )
    # 120 min sweet band at 10 s exposure -> 120 * 60 / 10 = 720 subs.
    assert plans[0].recommended_exposure_s == 10
    assert plans[0].recommended_subs == 720


# --- Phase 2: project-aware ranking (additive, backward-compatible) ---------

NOW = "2026-07-05T04:00:00Z"


def test_active_project_needing_data_outranks_fresh():
    # Two identical-observability emission targets. A is an active project with
    # 30 of 360 min collected (needs more); B is fresh. A must outrank B.
    site = SiteProfile(name="x", lat_deg=40, lon_deg=-74, bortle=6)
    cat = [
        DsoTarget("A", "A", 0, 0, "emission_nebula", 20, 7),
        DsoTarget("B", "B", 0, 0, "emission_nebula", 20, 7),
    ]
    obs_map = {"A": _obs(90), "B": _obs(90)}
    projects = {
        "A": Project(
            target_id="A",
            target_name="A",
            goal_minutes=360,
            collected_minutes=30,
            status="active",
            created_utc="2026-07-01T00:00:00Z",
            updated_utc="2026-07-01T00:00:00Z",
        )
    }
    plans = rank_targets(
        site,
        NOW,
        cat,
        _cond(),
        observability_fn=lambda s, t, w, dark_window_utc=None: obs_map[t.id],
        projects=projects,
        now_utc=NOW,
    )
    assert [p.target.id for p in plans][0] == "A"
    a_plan = next(p for p in plans if p.target.id == "A")
    assert any("active project" in r for r in a_plan.reasons)


def test_recently_imaged_suppressed():
    # C is a completed project imaged yesterday (not active-needing); D is fresh.
    # C must rank below D and carry a suppression reason.
    site = SiteProfile(name="x", lat_deg=40, lon_deg=-74, bortle=6)
    cat = [
        DsoTarget("C", "C", 0, 0, "emission_nebula", 20, 7),
        DsoTarget("D", "D", 0, 0, "emission_nebula", 20, 7),
    ]
    obs_map = {"C": _obs(90), "D": _obs(90)}
    projects = {
        "C": Project(
            target_id="C",
            target_name="C",
            goal_minutes=60,
            collected_minutes=60,
            status="complete",
            created_utc="2026-07-01T00:00:00Z",
            updated_utc="2026-07-04T04:00:00Z",
            sessions=[
                SessionRecord(
                    date_utc="2026-07-04T04:00:00Z",
                    integration_minutes=60,
                    subs_total=1,
                    subs_kept=1,
                )
            ],
        )
    }
    plans = rank_targets(
        site,
        NOW,
        cat,
        _cond(),
        observability_fn=lambda s, t, w, dark_window_utc=None: obs_map[t.id],
        projects=projects,
        now_utc=NOW,
    )
    order = [p.target.id for p in plans]
    assert order.index("C") > order.index("D")
    c_plan = next(p for p in plans if p.target.id == "C")
    joined = " ".join(c_plan.reasons)
    assert "imaged 1d ago" in joined or "goal met" in joined


def test_projects_none_matches_phase1():
    # Same inputs as test_more_sweet_band_time_ranks_higher, with projects=None,
    # must reproduce Phase 1 ordering, top score and reasons exactly.
    site = SiteProfile(name="x", lat_deg=40, lon_deg=-74, bortle=6)
    cat = [
        DsoTarget("A", "A", 0, 0, "emission_nebula", 20, 7),
        DsoTarget("B", "B", 0, 0, "emission_nebula", 20, 7),
    ]
    obs_map = {"A": _obs(120), "B": _obs(20)}

    def fn(s, t, w, dark_window_utc=None):
        return obs_map[t.id]

    base = rank_targets(site, NOW, cat, _cond(), observability_fn=fn)
    with_none = rank_targets(
        site, NOW, cat, _cond(), observability_fn=fn, projects=None
    )
    assert [p.target.id for p in base] == ["A", "B"]
    assert [p.target.id for p in with_none] == [p.target.id for p in base]
    assert with_none[0].score == base[0].score
    assert with_none[0].reasons == base[0].reasons


# --- Task 7 (2026-09-22 review): dark_window computed once per call ---------
# rank_targets used to let each of 120 catalog targets recompute dark_window
# inside observability. Task 6 widened dark_window's Sun grid (33 -> 64ms),
# taking 120x `observability` from 8.1s to 11.1s. dark_window is now computed
# once per rank_targets call and threaded into every observability_fn call as
# its own dark_window_utc.


def test_rank_targets_identical_with_and_without_precomputed_window():
    # Real observability_fn (the default) and real catalog targets, so this
    # exercises the actual astro.dark_window / observability wiring, not the
    # hand-built _obs fixture.
    from seestar_mcp.planning.astro import dark_window, observability

    site = SiteProfile(name="x", lat_deg=40.0, lon_deg=-74.0, bortle=6)
    when = "2026-07-05T04:00:00Z"
    cat = [find_target("M27"), find_target("M13")]

    window = dark_window(site, when)
    default_plans = rank_targets(site, when, cat, _cond())
    explicit_plans = rank_targets(site, when, cat, _cond(), dark_window_utc=window)

    assert [p.target.id for p in default_plans] == [
        p.target.id for p in explicit_plans
    ]
    for a, b in zip(default_plans, explicit_plans, strict=True):
        assert a.score == b.score
        assert a.observability == b.observability

    # And both agree with an independent per-target call, the old interface.
    for plan in default_plans:
        expected = observability(site, plan.target, when, dark_window_utc=None)
        assert plan.observability == expected


def test_dark_window_computed_once_per_rank_targets_call(monkeypatch):
    import seestar_mcp.planning.ranker as ranker_mod

    real_dark_window = ranker_mod.dark_window
    calls: list[int] = []

    def _counting(site, when):
        calls.append(1)
        return real_dark_window(site, when)

    monkeypatch.setattr(ranker_mod, "dark_window", _counting)

    site = SiteProfile(name="x", lat_deg=40.0, lon_deg=-74.0, bortle=6)
    cat = [find_target("M27"), find_target("M13"), find_target("M31")]
    rank_targets(site, "2026-07-05T04:00:00Z", cat, _cond())

    assert len(calls) == 1, f"dark_window called {len(calls)} times, expected 1"
