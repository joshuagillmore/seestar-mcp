"""Tests for the deterministic observability engine (astro.py).

Physics that is exact (the field-rotation formula) is hand-checked; astropy-
derived quantities (M27 transit altitude, moon values) are pinned to tolerances.
All calls take an explicit ``when_utc`` so the results are reproducible.
"""

from datetime import datetime

import pytest

from seestar_mcp.planning.astro import (
    _to_time,
    azalt_at,
    dark_window,
    field_rotation_rate,
    moon_illumination,
    observability,
    planning_when,
)
from seestar_mcp.planning.catalog import find_target
from seestar_mcp.planning.site import SiteProfile


def test_to_time_accepts_offset_suffixed_iso():
    # datetime.now(timezone.utc).isoformat() yields '...+00:00' with microseconds,
    # which the controller passes to the planner whenever date=None. astropy's
    # Time() rejects the offset, so this must be normalized, not passed through raw.
    a = _to_time("2026-07-06T01:37:39.165314+00:00")
    b = _to_time("2026-07-06T01:37:39.165314")
    assert abs((a - b).sec) < 1e-6
    # 'Z' must still work, and a non-zero offset must be converted to real UTC.
    assert abs((_to_time("2026-07-06T01:37:39Z") - _to_time("2026-07-06T01:37:39")).sec) < 1e-6
    assert abs((_to_time("2026-07-06T06:37:39+05:00") - _to_time("2026-07-06T01:37:39")).sec) < 1e-6
    # The real crash path: an offset-suffixed 'now' flowing through dark_window.
    site = SiteProfile(name="x", lat_deg=45.42, lon_deg=-75.70)
    dusk, dawn = dark_window(site, "2026-07-06T01:37:39.165314+00:00")
    assert dusk < dawn


def test_field_rotation_formula_hand_check():
    # 15.041 * cos(40) * cos(0) / cos(45) = 16.29 deg/hr (exact formula check).
    r = field_rotation_rate(lat_deg=40.0, az_deg=0.0, alt_deg=45.0)
    assert abs(r - 16.29) < 0.1
    # az=90 (due east) -> cos(az)=0 -> rate ~0.
    assert field_rotation_rate(40.0, 90.0, 45.0) < 0.01


def test_m27_transit_altitude_and_ceiling_flag():
    # transit alt ~ 90 - |lat - dec|; M27 dec +22.72, lat 40 -> ~72.7 deg (> 60 ceiling).
    site = SiteProfile(name="x", lat_deg=40.0, lon_deg=-74.0, bortle=6)
    obs = observability(site, find_target("M27"), "2026-07-05T04:00:00Z")
    assert 71.0 < obs.max_alt_deg < 74.0
    assert obs.transits_above_ceiling is True
    assert obs.dark_minutes_in_sweet_band <= obs.dark_minutes_above_floor
    assert 0.0 <= obs.moon_illum_frac <= 1.0


def test_dark_window_is_night():
    site = SiteProfile(name="x", lat_deg=40.0, lon_deg=-74.0)
    dusk, dawn = dark_window(site, "2026-07-05T04:00:00Z")
    assert dusk < dawn  # ISO strings compare lexically for same-format UTC


def test_moon_illumination_is_a_fraction():
    # Illuminated fraction is always a physical 0..1 value at any instant.
    frac = moon_illumination("2026-07-05T04:00:00Z")
    assert 0.0 <= frac <= 1.0


def test_azalt_at_single_instant_matches_transit():
    # The single-instant az/alt helper must agree with the observability engine:
    # evaluated at the target's transit time it returns the max altitude.
    site = SiteProfile(name="x", lat_deg=40.0, lon_deg=-74.0)
    t = find_target("M27")
    obs = observability(site, t, "2026-07-05T04:00:00Z")
    az, alt = azalt_at(site, t, obs.transit_utc)
    assert 0.0 <= az <= 360.0
    assert abs(alt - obs.max_alt_deg) < 0.5


# --- dark_window must never clip the night it selects (2026-09-22 review) -----
# The +/-12h Sun grid clipped whichever end of the night fell outside it: at
# 20:00Z (16:00 EDT) dawn came back as 08:00Z instead of ~09:25Z, and at 16:30Z
# the night started at the grid edge (04:30Z) instead of ~00:40Z. Reproduced at
# lat 38.9, lon -77.0. Times carry a 5-min tolerance: the Sun grid step.

DC = SiteProfile(name="t", lat_deg=38.9, lon_deg=-77.0, elevation_m=0.0)


def _near(actual_iso: str, expected_iso: str, tol_min: float = 5.0) -> bool:
    a = datetime.fromisoformat(actual_iso.replace("Z", "+00:00")).replace(tzinfo=None)
    e = datetime.fromisoformat(expected_iso)
    return abs((a - e).total_seconds()) <= tol_min * 60.0


def test_dark_window_afternoon_keeps_the_real_dawn():
    dusk, dawn = dark_window(DC, "2026-09-22T20:00:00Z")
    assert _near(dusk, "2026-09-23T00:35"), dusk
    assert _near(dawn, "2026-09-23T09:25"), f"dawn clipped to the grid edge: {dawn}"


def test_dark_window_midday_returns_the_whole_nearest_night():
    dusk, dawn = dark_window(DC, "2026-09-22T16:30:00Z")
    assert _near(dusk, "2026-09-22T00:40"), f"dusk clipped to the grid edge: {dusk}"
    assert _near(dawn, "2026-09-22T09:25"), dawn


def test_dark_window_after_dawn_is_the_just_ended_night():
    # check_night_guardrails relies on this: after dawn it must still see the
    # night that just ended, or the dawn stop would never fire.
    when = "2026-09-23T09:40:00Z"
    dusk, dawn = dark_window(DC, when)
    assert _near(dusk, "2026-09-23T00:35"), dusk
    assert _near(dawn, "2026-09-23T09:25"), dawn
    assert _to_time(dawn) < _to_time(when)


def test_dark_window_before_dusk_is_tonight():
    dusk, dawn = dark_window(DC, "2026-09-22T23:30:00Z")
    assert _near(dusk, "2026-09-23T00:35"), dusk
    assert _near(dawn, "2026-09-23T09:25"), dawn


def test_dark_window_in_the_dark_is_unchanged():
    # Exact, not toleranced: inside the night the old grid never clipped, so the
    # wider grid must reproduce the prior output to the sample.
    assert dark_window(DC, "2026-09-23T02:00:00Z") == (
        "2026-09-23T00:35:00.000",
        "2026-09-23T09:25:00.000",
    )


# --- planning_when: the planning tools plan the UPCOMING night (2026-09-22) ----
# The tools passed `date or now` straight into dark_window, whose nearest-night
# semantics pick LAST night from a morning or midday call, and a bare
# "2026-09-22" parsed to 00:00Z (20:00 EDT on the 21st), so it planned the night
# of the 21st. planning_when resolves the instant the planning tools should use.


def test_planning_when_morning_moves_to_tonight():
    when = planning_when(DC, "2026-09-22T12:00:00Z")
    dusk, dawn = dark_window(DC, when)
    assert _near(dusk, "2026-09-23T00:35"), dusk
    assert _near(dawn, "2026-09-23T09:25"), dawn


def test_planning_when_in_the_dark_is_unchanged():
    when = planning_when(DC, "2026-09-23T02:00:00Z")
    assert abs((_to_time(when) - _to_time("2026-09-23T02:00:00Z")).sec) < 1e-3


def test_planning_when_bare_date_is_that_evenings_night():
    when = planning_when(DC, "2026-09-22")
    dusk, dawn = dark_window(DC, when)
    assert _near(dusk, "2026-09-23T00:35"), dusk
    assert _near(dawn, "2026-09-23T09:25"), dawn


# The in-dark pin above (test_planning_when_in_the_dark_is_unchanged, 02:00Z ~=
# 22:00 EDT) is still before local midnight. The dashboard session (2026-09-24
# review) verified the rule at both sides of it: inside the dark window -> that
# night, unchanged, even after local midnight; after astronomical dawn -> the
# NEXT night, not the one that just ended.


def test_planning_when_after_local_midnight_still_in_the_dark_is_unchanged():
    # 05:00Z ~= 01:00 EDT: after LOCAL midnight, still inside the night that
    # began the previous evening.
    when = planning_when(DC, "2026-09-23T05:00:00Z")
    assert abs((_to_time(when) - _to_time("2026-09-23T05:00:00Z")).sec) < 1e-3
    dusk, dawn = dark_window(DC, when)
    assert _near(dusk, "2026-09-23T00:35"), dusk
    assert _near(dawn, "2026-09-23T09:25"), dawn


def test_planning_when_after_astronomical_dawn_moves_to_the_next_night():
    # Just after dawn: the night that began the Sep 22 evening has ended, so
    # planning_when must resolve to the NEXT night (Sep 23 evening -> Sep 24
    # morning), not the one dark_window still reports as "nearest" (see
    # test_dark_window_after_dawn_is_the_just_ended_night above).
    when = planning_when(DC, "2026-09-23T09:40:00Z")
    dusk, dawn = dark_window(DC, when)
    assert _near(dusk, "2026-09-24T00:35"), dusk
    assert _near(dawn, "2026-09-24T09:25"), dawn


# lat 65 at the June solstice never reaches astro dark: dark_window takes its
# "within 1 deg of the darkest sample" fallback, which idempotency must survive.
HIGH_SUMMER = SiteProfile(name="h", lat_deg=65.0, lon_deg=-18.0, elevation_m=0.0)


@pytest.mark.parametrize(
    ("site", "when"),
    [
        (DC, "2026-09-22T12:00:00Z"),
        (DC, "2026-09-22T16:30:00Z"),
        (DC, "2026-09-22T20:00:00Z"),
        (DC, "2026-09-22T23:30:00Z"),
        (DC, "2026-09-23T02:00:00Z"),
        (DC, "2026-09-23T09:40:00Z"),
        (DC, "2026-09-22T12:00:00.123456+00:00"),  # the real-clock `now` shape
        (DC, "2026-09-22"),
        (HIGH_SUMMER, "2026-06-21T12:00:00Z"),
        (HIGH_SUMMER, "2026-06-21T03:00:00Z"),
        (HIGH_SUMMER, "2026-06-21"),
    ],
)
def test_planning_when_is_idempotent_and_inside_its_night(site, when):
    first = planning_when(site, when)
    assert planning_when(site, first) == first
    # ...because the resolved instant lies inside the window it selects, so
    # dark_window / observability / moon_illumination all agree on the night.
    dusk, dawn = dark_window(site, first)
    assert _to_time(dusk) <= _to_time(first) <= _to_time(dawn)


def test_high_summer_site_really_takes_the_fallback():
    # Guards the fixture above: if this site ever reached astro dark, the
    # idempotency cases would no longer exercise the fallback span.
    from seestar_mcp.planning.astro import _sun_alt_grid

    _, alt = _sun_alt_grid(HIGH_SUMMER, _to_time(planning_when(HIGH_SUMMER, "2026-06-21")))
    assert alt.min() > -18.0


# --- observability(..., dark_window_utc=...): additive, skips recompute -------
# Task 7 (2026-09-22 review): dark_window recomputed once per target inside
# observability took 120x `observability` in rank_targets from 8.1s to 11.1s
# after Task 6 widened the Sun grid. dark_window_utc lets a caller that already
# has the window (rank_targets, computing it once for the whole catalog) hand it
# straight to observability instead.


def test_observability_dark_window_utc_none_matches_old_output():
    site = SiteProfile(name="x", lat_deg=40.0, lon_deg=-74.0, bortle=6)
    t = find_target("M27")
    when = "2026-07-05T04:00:00Z"
    old = observability(site, t, when)
    new = observability(site, t, when, dark_window_utc=None)
    assert new == old


def test_observability_precomputed_window_skips_recompute_and_matches(monkeypatch):
    import seestar_mcp.planning.astro as astro_mod

    site = SiteProfile(name="x", lat_deg=40.0, lon_deg=-74.0, bortle=6)
    t = find_target("M27")
    when = "2026-07-05T04:00:00Z"

    baseline = observability(site, t, when)
    window = dark_window(site, when)

    real_dark_window = astro_mod.dark_window
    calls: list[int] = []

    def _counting(*a, **k):
        calls.append(1)
        return real_dark_window(*a, **k)

    monkeypatch.setattr(astro_mod, "dark_window", _counting)
    obs = observability(site, t, when, dark_window_utc=window)

    assert calls == [], "observability must not recompute dark_window when it is given"
    assert obs == baseline
