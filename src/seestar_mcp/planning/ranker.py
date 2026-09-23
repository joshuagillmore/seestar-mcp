"""Reasoned target ranker for the observing planner.

Fuses the deterministic :class:`~seestar_mcp.planning.astro.Observability`
record with light-pollution suitability, moon geometry and framing into a single
0..100 score per target, and explains every score in ``TargetPlan.reasons``.

Design notes (all weights are documented module constants so the blend is tunable
without touching the math):

* The **primary** driver is bankable clean time in the field-rotation sweet band
  (``dark_minutes_in_sweet_band``). Raw high altitude is *not* rewarded — a
  near-zenith transit rotates fastest exactly when it is best placed, so we lean
  on the sweet-band minutes and a field-rotation-health term instead.
* **Sub-count (corrected):** ``recommended_subs`` is the total clean sweet-band
  time divided by the exposure — ``int(dark_minutes_in_sweet_band * 60 /
  recommended_exposure_s)``. It is deliberately NOT scaled by
  ``usable_sub_minutes`` (which is the at-transit worst-case per-sub smear
  window, tiny for near-zenith targets, not a total-time budget).
* Targets with zero sweet-band time are dropped entirely.

This module is pure/local: no clock, no network. ``observability_fn`` is
injectable so tests stay deterministic without astropy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from .astro import Observability, dark_window, observability
from .lightpollution import bortle_for, lp_suitability

if TYPE_CHECKING:
    from .catalog import DsoTarget
    from .projects import Project
    from .site import SiteProfile
    from .weather import ConditionsAssessment

# --- Scoring weights (0..1 blend, sum to 1.0). Documented + tunable. ---------
#: Bankable clean time in the sweet band — the primary driver.
W_SWEET_BAND = 0.40
#: Light-pollution fit of the target type at the site's Bortle class.
W_LP_FIT = 0.20
#: Moon term: far from a dim moon is best; near a bright moon hurts.
W_MOON = 0.15
#: Framing fit: how well the target's angular size suits the Seestar FOV.
W_FRAMING = 0.10
#: Field-rotation health: fraction of usable time that is actually in the sweet
#: band (a target that only clears the horizon above the ceiling scores low).
W_FIELD_ROT = 0.15

#: Sweet-band minutes that normalize the time term to 1.0 (a full clean night).
SWEET_BAND_FULL_MINUTES = 240.0

#: Seestar S50 field of view long dimension (~1.3 deg x 0.7 deg ~= 78' x 42').
FOV_LONG_ARCMIN = 78.0
#: Below this angular size a target is small in the frame.
FOV_SMALL_ARCMIN = 5.0

#: Default Seestar sub exposure (seconds). Recorded on every plan.
RECOMMENDED_EXPOSURE_S = 10

# --- Project-awareness adjustments (0..1 blend units; only when a projects dict
# --- is supplied — with ``projects=None`` none of this runs, so ranking is
# --- byte-identical to Phase 1). Bounded constants → clamped back to 0..100. ---
#: Bonus for an active project still short of its goal ("needs more data").
PROJECT_BONUS = 0.10
#: Penalty for a completed project or one imaged within ``recent_days``.
PROJECT_PENALTY = -0.15


@dataclass
class TargetPlan:
    """A scored, reasoned plan for one target on one night (JSON-serializable)."""

    target: DsoTarget
    score: int  # 0..100
    reasons: list[str]  # every score is explained
    best_window_utc: tuple[str, str] | None
    recommended_subs: int
    recommended_exposure_s: int
    framing_note: str
    observability: Observability


def _framing_note(size_arcmin: float) -> str:
    """Describe how ``size_arcmin`` sits in the Seestar FOV."""
    if size_arcmin > FOV_LONG_ARCMIN:
        return "larger than FOV — consider a mosaic"
    if size_arcmin < FOV_SMALL_ARCMIN:
        return "small in FOV"
    return f"fits FOV ({size_arcmin:g}')"


def _framing_fit(size_arcmin: float) -> float:
    """0..1 framing-fit term: best when the target comfortably fills the frame."""
    if size_arcmin > FOV_LONG_ARCMIN:
        # Too big for one frame; falls off as it grows past the FOV.
        return max(0.3, FOV_LONG_ARCMIN / size_arcmin)
    if size_arcmin < FOV_SMALL_ARCMIN:
        # Tiny in frame; scales up toward the small-target threshold.
        return max(0.3, size_arcmin / FOV_SMALL_ARCMIN)
    return 1.0


def _score_target(
    site: SiteProfile,
    target: DsoTarget,
    obs: Observability,
    blend_delta: float = 0.0,
) -> tuple[int, float, float, float, float]:
    """Return ``(score, time_term, lp_fit, moon_term, field_rot_term)``.

    ``blend_delta`` is an optional bounded project adjustment (in 0..1 blend
    units) added before the final clamp/scale. With the default ``0.0`` the math
    is byte-identical to Phase 1.
    """
    time_term = min(1.0, obs.dark_minutes_in_sweet_band / SWEET_BAND_FULL_MINUTES)
    lp_fit = lp_suitability(target.type, bortle_for(site))
    moon_term = min(1.0, obs.moon_sep_deg / 90.0) * (1.0 - 0.5 * obs.moon_illum_frac)
    framing = _framing_fit(target.size_arcmin)
    field_rot = obs.dark_minutes_in_sweet_band / max(1.0, obs.dark_minutes_above_floor)

    blend = (
        W_SWEET_BAND * time_term
        + W_LP_FIT * lp_fit
        + W_MOON * moon_term
        + W_FRAMING * framing
        + W_FIELD_ROT * field_rot
    )
    score = int(max(0, min(100, round((blend + blend_delta) * 100))))
    return score, time_term, lp_fit, moon_term, field_rot


def _reasons(
    site: SiteProfile,
    target: DsoTarget,
    obs: Observability,
    lp_fit: float,
    framing_note: str,
    recommended_subs: int,
) -> list[str]:
    """Name every contributing factor behind a target's score."""
    reasons: list[str] = []

    if obs.best_window_utc is not None:
        start, end = obs.best_window_utc
        reasons.append(f"best window {start}–{end} UTC")

    reasons.append(
        f"{obs.dark_minutes_in_sweet_band:.0f} min clean sweet-band time"
        f" ({recommended_subs} subs at {RECOMMENDED_EXPOSURE_S}s)"
    )

    if obs.transits_above_ceiling:
        reasons.append(
            f"transit {obs.max_alt_deg:.0f}° — field rotation above ceiling"
        )
        # Real-hardware finding: clean 10 s subs bank fine down to ~73° alt, so
        # sub-trailing is only a genuine concern for near-zenith (above-ceiling)
        # transits. Gate the trail note on that flag rather than the conservative
        # smear threshold, which otherwise fires on nearly every target.
        reasons.append("subs trail near transit")

    reasons.append(
        f"{obs.moon_sep_deg:.0f}° from a {obs.moon_illum_frac * 100:.0f}%-lit moon"
    )

    reasons.append(
        f"{target.type.replace('_', ' ')} suits Bortle {bortle_for(site)}"
        f" (LP fit {lp_fit:.2f})"
    )

    reasons.append(framing_note)
    return reasons


def _parse_iso(value: str) -> datetime:
    """Parse an ISO timestamp, tolerating a trailing ``Z`` (→ ``+00:00``)."""
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def _days_since_last_session(proj: Project, now_utc: str | None) -> int | None:
    """Whole-day delta from ``now_utc`` to the project's most-recent session.

    Returns ``None`` when ``now_utc`` is missing or no session date parses. Uses
    the passed-in project's ``sessions`` inline (no disk read).
    """
    if now_utc is None:
        return None
    now = _parse_iso(now_utc)
    latest: datetime | None = None
    for session in proj.sessions:
        try:
            when = _parse_iso(session.date_utc)
        except (ValueError, TypeError, AttributeError):
            continue
        if latest is None or when > latest:
            latest = when
    if latest is None:
        return None
    return (now - latest).days


def _project_adjustment(
    target: DsoTarget,
    projects: dict[str, Project],
    now_utc: str | None,
    recent_days: int,
) -> tuple[float, list[str]]:
    """Return ``(blend_delta, extra_reasons)`` for a target given its project.

    A target is either **boosted** (active project still short of its goal),
    **suppressed** (imaged within ``recent_days``, or a completed goal), or
    neutral. Never raises: a malformed project entry yields no adjustment.
    """
    try:
        proj = projects.get(target.id)
        if proj is None:
            return 0.0, []

        goal = proj.goal_minutes
        collected = proj.collected_minutes
        active_needing = proj.status == "active" and (
            goal == 0 or collected < goal
        )
        if active_needing:
            if goal == 0:
                reason = (
                    f"active project — {collected:.0f} min collected, open-ended"
                )
            else:
                reason = (
                    f"active project — {collected:.0f} of {goal:.0f} min,"
                    " needs more"
                )
            return PROJECT_BONUS, [reason]

        # Not active-needing: suppress if recently imaged, else if goal is met.
        days = _days_since_last_session(proj, now_utc)
        if days is not None and 0 <= days <= recent_days:
            return PROJECT_PENALTY, [f"imaged {days}d ago"]

        completed = proj.status == "complete" or (goal > 0 and collected >= goal)
        if completed:
            return PROJECT_PENALTY, ["goal met"]

        return 0.0, []
    except Exception:  # noqa: BLE001 - a bad project entry must not crash ranking
        return 0.0, []


def _plan_target(
    site: SiteProfile,
    target: DsoTarget,
    obs: Observability,
    *,
    blend_delta: float = 0.0,
    extra_reasons: list[str] | None = None,
) -> TargetPlan:
    """Build a fully-reasoned :class:`TargetPlan` for one scored target."""
    score, _time, lp_fit, _moon, _frot = _score_target(site, target, obs, blend_delta)

    recommended_subs = int(
        obs.dark_minutes_in_sweet_band * 60.0 / RECOMMENDED_EXPOSURE_S
    )
    framing_note = _framing_note(target.size_arcmin)
    reasons = _reasons(site, target, obs, lp_fit, framing_note, recommended_subs)
    if extra_reasons:
        reasons.extend(extra_reasons)

    return TargetPlan(
        target=target,
        score=score,
        reasons=reasons,
        best_window_utc=obs.best_window_utc,
        recommended_subs=recommended_subs,
        recommended_exposure_s=RECOMMENDED_EXPOSURE_S,
        framing_note=framing_note,
        observability=obs,
    )


def rank_targets(
    site: SiteProfile,
    when_utc: str,
    catalog: list[DsoTarget],
    conditions: ConditionsAssessment,
    *,
    types: list[str] | None = None,
    min_alt: float | None = None,
    limit: int | None = None,
    dark_window_utc: tuple[str, str] | None = None,
    observability_fn: Callable[..., Observability] = observability,
    projects: dict[str, Project] | None = None,
    now_utc: str | None = None,
    recent_days: int = 2,
) -> list[TargetPlan]:
    """Rank ``catalog`` for ``site`` on ``when_utc``, best score first.

    Filters by ``types`` (if given), computes observability for each surviving
    target via ``observability_fn`` (injectable for deterministic tests), drops
    any target with no sweet-band time, scores the rest on a documented weighted
    blend and returns them sorted descending by score (top ``limit`` if given).

    ``conditions`` is accepted for the conditions caveat / API symmetry; the
    go/no-go verdict does not exclude targets (planning still runs on a no-go
    night — the caller annotates the caveat). Never raises.

    **Dark window, computed once (2026-09-22 review, Task 7):** the night's
    ``(dusk, dawn)`` pair is resolved ONCE per call — from ``dark_window_utc`` if
    the caller already has it (``plan_targets`` needs the same window for its
    weather assessment), else freshly via :func:`~.astro.dark_window` — and
    passed to every ``observability_fn`` call as its own ``dark_window_utc``
    rather than letting each of up to 120 catalog targets recompute it (that
    took 120x `observability` from 8.1s to 11.1s after Task 6 widened
    ``dark_window``'s Sun grid).

    **Project-awareness (optional, backward-compatible):** when ``projects`` (a
    ``{target_id: Project}`` map, already loaded — this function never reads
    disk) is supplied, an active project still short of its goal gets a bounded
    score boost, while a completed project or one imaged within ``recent_days``
    (measured against ``now_utc``) is suppressed, each with an explaining reason.
    With ``projects=None`` (the default) none of this runs and the result is
    byte-identical to Phase 1.
    """
    try:
        selected = catalog
        if types:
            wanted = set(types)
            selected = [t for t in catalog if t.type in wanted]

        window = (
            dark_window_utc
            if dark_window_utc is not None
            else dark_window(site, when_utc)
        )

        plans: list[TargetPlan] = []
        for target in selected:
            try:
                obs = observability_fn(site, target, when_utc, dark_window_utc=window)
            except Exception:  # noqa: BLE001 - a bad target must not sink the batch
                continue
            if obs.dark_minutes_in_sweet_band <= 0:
                continue  # never up / no clean sweet-band time — excluded
            blend_delta = 0.0
            extra_reasons: list[str] | None = None
            if projects is not None:
                blend_delta, extra_reasons = _project_adjustment(
                    target, projects, now_utc, recent_days
                )
            plans.append(
                _plan_target(
                    site,
                    target,
                    obs,
                    blend_delta=blend_delta,
                    extra_reasons=extra_reasons,
                )
            )

        plans.sort(key=lambda p: p.score, reverse=True)
        if limit is not None and limit >= 0:
            plans = plans[:limit]
        return plans
    except Exception:  # noqa: BLE001 - tool-facing never-raise contract
        return []
