"""Tests for the deterministic observability engine (astro.py).

Physics that is exact (the field-rotation formula) is hand-checked; astropy-
derived quantities (M27 transit altitude, moon values) are pinned to tolerances.
All calls take an explicit ``when_utc`` so the results are reproducible.
"""

from datetime import datetime
from math import cos, radians

import pytest

from seestar_mcp.planning.astro import (
    _to_time,
    azalt_at,
    dark_window,
    field_rotation_rate,
    j2000_to_jnow,
    jnow_to_j2000,
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


# --- J2000 <-> JNow (goto epoch fix, 2026-09-24) -----------------------------
# Live test 2026-09-23/24 (fw 8.46): objects imaged through the MCP landed
# 8-23' off-centre while the phone app centred them. goto_target sent J2000
# catalog coordinates, but the firmware works in JNow (mean equator and
# equinox of date). The measured offsets matched J2000->JNow precession:
# M92 9.1' predicted / 9.2' measured, M57 12.7'/12.6', M1 22.4'/22.5'.

_M1_J2000 = (83.633, 22.017)  # catalog M1, degrees
_EPOCH_ISO = "2026-09-24T04:00:00Z"


def _sep_arcsec(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle separation of two (ra_deg, dec_deg) points, in arcseconds."""
    from math import acos, degrees, sin

    (ra1, d1), (ra2, d2) = a, b
    c = sin(radians(d1)) * sin(radians(d2)) + cos(radians(d1)) * cos(
        radians(d2)
    ) * cos(radians(ra1 - ra2))
    return degrees(acos(max(-1.0, min(1.0, c)))) * 3600.0


def _dra_cos_arcmin(ra_new: float, ra_old: float, dec_deg: float) -> float:
    """RA change (wrapped to +/-180 deg) times cos(dec), in arcminutes."""
    d = (ra_new - ra_old + 180.0) % 360.0 - 180.0
    return d * 60.0 * cos(radians(dec_deg))


def test_j2000_to_jnow_m1_matches_the_measured_precession_offset():
    """M1 at 2026-09-24T04:00Z: dRA*cos(dec) ~ +22.4' east, dDec ~ +1.0'."""
    ra, dec = j2000_to_jnow(*_M1_J2000, _EPOCH_ISO)
    assert _dra_cos_arcmin(ra, _M1_J2000[0], _M1_J2000[1]) == pytest.approx(
        22.4, abs=0.2
    )
    assert (dec - _M1_J2000[1]) * 60.0 == pytest.approx(1.0, abs=0.2)
    # And the total shift is the 22.4' predicted for the live-test table.
    assert _sep_arcsec((ra, dec), _M1_J2000) / 60.0 == pytest.approx(22.4, abs=0.2)


def test_jnow_to_j2000_m1_undoes_the_shift():
    jnow = j2000_to_jnow(*_M1_J2000, _EPOCH_ISO)
    back = jnow_to_j2000(*jnow, _EPOCH_ISO)
    assert back[0] == pytest.approx(_M1_J2000[0], abs=1e-6)
    assert back[1] == pytest.approx(_M1_J2000[1], abs=1e-6)


@pytest.mark.parametrize(
    "ra, dec",
    [
        (83.633, 22.017),  # M1
        (283.396, 33.029),  # M57
        (10.68, 41.27),  # M31
        (202.4696, 47.1952),  # M51
        (0.0, 0.0),
        (359.9, -45.0),
        (180.0, 85.0),
        (45.0, -89.5),
    ],
)
def test_j2000_jnow_round_trip_both_ways_better_than_0p1_arcsec(ra, dec):
    fwd = j2000_to_jnow(ra, dec, _EPOCH_ISO)
    assert _sep_arcsec(jnow_to_j2000(*fwd, _EPOCH_ISO), (ra, dec)) < 0.1
    inv = jnow_to_j2000(ra, dec, _EPOCH_ISO)
    assert _sep_arcsec(j2000_to_jnow(*inv, _EPOCH_ISO), (ra, dec)) < 0.1


@pytest.mark.parametrize("dec", [85.0, 89.99, 90.0, -85.0, -90.0])
def test_j2000_jnow_near_the_poles_never_raises(dec):
    for convert in (j2000_to_jnow, jnow_to_j2000):
        ra_out, dec_out = convert(123.4, dec, _EPOCH_ISO)
        assert isinstance(ra_out, float) and isinstance(dec_out, float)
        assert 0.0 <= ra_out < 360.0
        assert -90.0 <= dec_out <= 90.0
    # Dec 85 still precesses by a sane amount (under half a degree in 27 yr).
    ra_now, dec_now = j2000_to_jnow(10.0, 85.0, _EPOCH_ISO)
    assert _sep_arcsec((ra_now, dec_now), (10.0, 85.0)) / 60.0 < 30.0


def test_j2000_to_jnow_wraps_ra_past_360_into_0_to_360():
    # RA grows under precession here, so 359.9 deg J2000 lands just past 0.
    ra, dec = j2000_to_jnow(359.9, 10.0, _EPOCH_ISO)
    assert 0.0 <= ra < 1.0, f"RA must wrap to [0, 360), got {ra}"
    assert _sep_arcsec((ra, dec), (359.9, 10.0)) / 60.0 == pytest.approx(22.1, abs=0.2)


def test_jnow_to_j2000_wraps_ra_below_0_into_0_to_360():
    ra, _dec = jnow_to_j2000(0.05, 10.0, _EPOCH_ISO)
    assert 359.0 < ra < 360.0, f"RA must wrap to [0, 360), got {ra}"
    ra0, _ = jnow_to_j2000(0.0, -30.0, _EPOCH_ISO)
    assert 359.0 < ra0 < 360.0


def test_j2000_jnow_is_deterministic_in_when_utc():
    from astropy.time import Time

    a = j2000_to_jnow(*_M1_J2000, _EPOCH_ISO)
    assert j2000_to_jnow(*_M1_J2000, _EPOCH_ISO) == a
    # The same instant in any accepted spelling gives the same answer.
    b = j2000_to_jnow(*_M1_J2000, "2026-09-24T04:00:00+00:00")
    c = j2000_to_jnow(*_M1_J2000, Time("2026-09-24T04:00:00", scale="utc"))
    assert b == pytest.approx(a, abs=1e-9)
    assert c == pytest.approx(a, abs=1e-9)
    # A different instant gives a different (larger, later) shift.
    later = j2000_to_jnow(*_M1_J2000, "2027-09-24T04:00:00Z")
    assert _sep_arcsec(later, _M1_J2000) > _sep_arcsec(a, _M1_J2000) + 30.0


def test_j2000_to_jnow_at_the_j2000_epoch_is_the_identity():
    # J2000.0 = 2000-01-01T12:00:00 TT = 11:58:55.816 UTC: no precession yet.
    ra, dec = j2000_to_jnow(*_M1_J2000, "2000-01-01T11:58:55.816")
    assert _sep_arcsec((ra, dec), _M1_J2000) < 0.1


@pytest.mark.parametrize(
    "ra, dec",
    [(83.6, 95.0), (83.6, -90.5), (float("nan"), 22.0), (83.6, float("inf"))],
)
def test_j2000_jnow_rejects_out_of_range_input_with_value_error(ra, dec):
    for convert in (j2000_to_jnow, jnow_to_j2000):
        with pytest.raises(ValueError):
            convert(ra, dec, _EPOCH_ISO)
