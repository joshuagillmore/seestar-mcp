"""FastMCP server for seestar-mcp: 33 auditable Seestar S50 control/QA/planning tools.

Two layers, deliberately separated for testability:

- :class:`SeestarController` — plain-async business logic. Every method returns a
  JSON-serializable dict and catches :class:`AlpacaError`, returning
  ``{"ok": False, "error": ...}`` instead of raising. This is unit-testable
  without any MCP machinery.
- The thin ``@mcp.tool()`` wrappers below — one per controller method, each a
  one-line ``return await get_controller().<method>(...)``. Tool docstrings are
  the tool *descriptions* the model sees, so they are written to be honest and
  explicit about side effects (anti tool-poisoning: a misleading description is a
  security defect, not just a docs nit).

Transport / network posture
----------------------------
``main()`` runs FastMCP over **stdio** (``mcp.run()`` default). This server opens
**no inbound network port**: Claude Code spawns it as a subprocess and speaks
stdio, and (for a Remote Control session) must register it *before* the session
starts. The only network egress is the outbound HTTP this process makes to
``seestar_alp`` (:5555) and the device data ports — those localhost/LAN-only bind
concerns are handled in :mod:`seestar_mcp.config` and
:mod:`seestar_mcp.data_client`, not here. No secret is imported into any tool
signature; credentials live only in :mod:`seestar_mcp.secrets`.
"""

from __future__ import annotations

import asyncio
import time
import dataclasses
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

from .alpaca_client import AlpacaClient, AlpacaError, AlpacaNotImplemented
from .data_client import DataClient
from .planning.astro import (
    azalt_at,
    dark_window,
    moon_illumination,
    observability,
    planning_when,
)
from .planning.autonomous import evaluate_guardrails, plan_night
from .planning.catalog import find_target, load_catalog
from .planning.obstructions import (
    location_status,
    record_sky_result,
    suggest_obstructions,
)
from .planning.projects import (
    get_project as _get_project,
    load_projects,
    log_session_result as _log_session_result,
    recommend_projects as _recommend_projects,
    upsert_project,
)
from .planning.ranker import rank_targets
from .planning.site import SiteProfile, is_blocked, load_site, save_site
from .planning.weather import assess_conditions as assess_conditions_weather
from .provenance import ProvenanceLog, SessionManifest
from .run_state import RunState, clear_run_state, read_run_state, write_run_state
from .qa_tier1 import Tier1Monitor
from .qa_tier2 import analyze_session, write_report

if TYPE_CHECKING:
    from .config import Settings


def _slug(text: str) -> str:
    """Filesystem/id-safe slug from a target name (letters, digits, dashes)."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text.strip()).strip("-").lower()
    return slug or "session"


class SeestarController:
    """Testable business logic behind the MCP tools.

    Holds the wired clients plus mutable per-session state (``session_id``,
    ``manifest``, ``target``). Async methods each return a JSON-serializable dict
    and never raise :class:`AlpacaError` — they catch it and return
    ``{"ok": False, "error": ..., "error_number": ...}``.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        provenance: ProvenanceLog,
        alpaca: AlpacaClient,
        data: DataClient,
        tier1: Tier1Monitor,
    ) -> None:
        self.settings = settings
        self.provenance = provenance
        self.alpaca = alpaca
        self.data = data
        self.tier1 = tier1
        #: TTL cache for the weather assessment: (key, monotonic_deadline, value).
        #: Lives in the TOOL layer on purpose — ``planning/`` must not read the
        #: clock (see CLAUDE.md), and this needs one.
        self._weather_cache: tuple[tuple, float, Any] | None = None

        # Per-session state.
        self.session_id: str | None = None
        self.manifest: SessionManifest | None = None
        self.target: str | None = None

    async def _weather_cached(self, site, window, illum):
        """Weather assessment, reused within ``weather_cache_ttl_s``.

        Why this exists: ``check_night_guardrails`` consumes exactly ONE value
        from the assessment — the tri-state ``weather_go`` — but each call used
        to issue a fresh two-package meteoblue request (``basic-1h_clouds-3h``)
        spanning the whole dark window. On 2026-07-31 a dashboard polled the
        guardrail once a minute for eleven hours, straight through dawn: 951
        uncached forecast fetches in one day against 1-5 on every other day.

        Forecasts do not move minute to minute, so one fetch is reused for the
        whole TTL. The key includes the site and window, so a real change (the
        scope moves, a new night) still refetches immediately.
        """
        # The window bounds are recomputed from `now` on every call, so they
        # drift by seconds continuously — keying on them verbatim defeats the
        # cache entirely (measured: 10 fetches for 10 polls). Truncate each
        # bound to the hour: a forecast whose window moved 40 seconds is the
        # same forecast, while a genuinely different night still misses.
        key = (
            round(float(site.lat_deg), 3),
            round(float(site.lon_deg), 3),
            tuple(str(b)[:13] for b in window),  # 'YYYY-MM-DDTHH'
            round(float(illum), 1),
        )
        now = time.monotonic()
        cached = self._weather_cache
        if cached is not None and cached[0] == key and now < cached[1]:
            return cached[2]
        value = await assess_conditions_weather(
            site, window, illum, api_key=self.settings.meteoblue_api_key
        )
        ttl = max(0.0, float(self.settings.weather_cache_ttl_s))
        self._weather_cache = (key, now + ttl, value)
        return value

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> SeestarController:
        """Build a fully-wired controller from ``Settings`` (default: cached)."""
        if settings is None:
            from .config import get_settings

            settings = get_settings()
        provenance = ProvenanceLog(
            settings.provenance_log, client_id=settings.client_id or None
        )
        alpaca = AlpacaClient.from_settings(settings, provenance)
        data = DataClient.from_settings(settings, alpaca, provenance)
        tier1 = Tier1Monitor(alpaca, provenance=provenance)
        return cls(
            settings,
            provenance=provenance,
            alpaca=alpaca,
            data=data,
            tier1=tier1,
        )

    # --- internal helpers -------------------------------------------------

    def _config_summary(self) -> dict[str, Any]:
        """Non-secret settings subset recorded into a session manifest.

        Only endpoints/thresholds — never a secret. The manifest additionally
        runs this through ``redact`` on construction as defence in depth.
        """
        s = self.settings
        return {
            "alpaca_base_url": s.alpaca_base_url,
            "alpaca_device_num": s.alpaca_device_num,
            "data_dir": str(s.data_dir),
            "manifest_dir": str(s.manifest_dir),
            "qa_fwhm_sigma": s.qa_fwhm_sigma,
            "qa_eccentricity_reject": s.qa_eccentricity_reject,
            "qa_snr_floor_factor": s.qa_snr_floor_factor,
            "qa_starcount_floor_factor": s.qa_starcount_floor_factor,
        }

    @staticmethod
    async def _maybe(coro: Any) -> Any:
        """Await ``coro``; on ``AlpacaNotImplemented`` return None.

        Used by :meth:`get_status` so the ~4-of-52 GETs the Seestar lacks resolve
        to ``None`` for that field rather than failing the whole status read.
        Other :class:`AlpacaError`s propagate to the method-level guard.
        """
        try:
            return await coro
        except AlpacaNotImplemented:
            return None

    # --- control / state --------------------------------------------------

    async def connect_telescope(self) -> dict:
        """Connect to the telescope via seestar_alp (Alpaca ``connected``).

        ``ok`` reflects whether the scope is CONNECTED, not merely whether the
        call completed. This used to return ``{"ok": True, "connected": False}``
        on a scope that never came up — truthful in the payload, but anything
        branching on ``ok`` alone (a run-book, a dashboard) concluded the scope
        was live. For an action tool named ``connect_*``, failing to connect is a
        failed action. ``connected`` is still carried either way.
        """
        try:
            await self.alpaca.set_connected(True)
            connected = await self._maybe(self.alpaca.get_connected())
            if connected:
                return {"ok": True, "connected": True}
            return {
                "ok": False,
                "connected": connected,
                "error": (
                    "connect attempt completed but the scope is still not "
                    "connected — it may be asleep, off the network, or the "
                    "firmware 7.18+ interop handshake may have failed "
                    "(see the authentication branch in anomaly-playbook)"
                ),
            }
        except AlpacaError as exc:
            return _err(exc)

    async def get_status(self) -> dict:
        """Read connection + pointing + tracking/slewing state.

        Each Alpaca field is read independently; a ``NotImplemented`` GET
        (expected for a few standard ASCOM properties the Seestar lacks)
        resolves to ``None``.

        ``tracking`` is ALPACA's view, which disagrees with the device on fw 7.75
        and 8.46. ``mount_parked`` / ``mount_tracking`` come from one native
        ``get_device_state`` call (``result.mount.close`` / ``.tracking``) and
        are the authoritative fields; see :func:`_parse_mount_state`.
        """
        try:
            self.provenance.log_call(tool="get_status", args={})
            status = {
                "connected": await self._maybe(self.alpaca.get_connected()),
                "rightascension": await self._maybe(self.alpaca.get_ra()),
                "declination": await self._maybe(self.alpaca.get_dec()),
                "tracking": await self._maybe(self.alpaca.get_tracking()),
                "slewing": await self._maybe(self.alpaca.is_slewing()),
            }
            # Task 5 (2026-09-22 review remediation): no tool exposed the native
            # mount state, so the skills could not confirm a park — Alpaca's
            # /atpark and /tracking disagree with the device on this hardware.
            # Best-effort and additive: any failure is unknown (None), never a
            # failed get_status.
            try:
                dev = await self.alpaca.method_sync("get_device_state")
                parked, mount_tracking = _parse_mount_state(dev)
            except Exception:  # noqa: BLE001 - advisory read, never fatal
                parked, mount_tracking = (None, None)
            status["mount_parked"] = parked
            status["mount_tracking"] = mount_tracking
            return {"ok": True, **status}
        except AlpacaError as exc:
            return _err(exc)

    async def get_view_state(self) -> dict:
        """Read the device's live ``get_view_state`` telemetry (native method).

        Adds ``observing`` and ``stack`` (see :func:`_summarize_view_state`)
        alongside the unchanged raw ``view_state`` -- live test 2026-09-24 found
        every framing/drop check needed a scratch script to dig
        ``stacked_frame``/``dropped_frame``/``frame_errcode`` and the Annotate
        pixel position out of the raw payload by hand.
        """
        try:
            self.provenance.log_call(tool="get_view_state", args={})
            state = await self.alpaca.method_sync("get_view_state")
            if (bad := _native_fail(state)) is not None:
                return bad
            observing, stack = _summarize_view_state(state)
            return {
                "ok": True,
                "view_state": state,
                "observing": observing,
                "stack": stack,
                "warning": _native_warning(state),
            }
        except AlpacaError as exc:
            return _err(exc)

    async def goto_target(
        self,
        name: str,
        ra: float,
        dec: float,
        use_lp_filter: bool = False,
        session_id: str | None = None,
    ) -> dict:
        """Slew to a target and start a session (creates the session manifest).

        This commands telescope MOTION. It derives a deterministic session id
        (target + UTC timestamp, unless ``session_id`` is supplied), opens a
        :class:`SessionManifest`, and issues the native goto.
        """
        try:
            if session_id is None:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                session_id = f"{_slug(name)}-{stamp}"
            self.session_id = session_id
            self.target = name
            # Persist the run so a restarted server can answer "is a run in
            # progress?" without inference. session_id alone was in-memory, so
            # five server deaths in one night left no successor able to resume or
            # wind down. Best-effort: a failure here must never block a goto.
            try:
                resolved = find_target(name)
                self._run_start_utc = getattr(self, "_run_start_utc", None) or (
                    datetime.now(timezone.utc).isoformat()
                )
                write_run_state(
                    RunState(
                        session_start_utc=self._run_start_utc,
                        target=name,
                        resolved_id=resolved.id if resolved is not None else None,
                    ),
                    self._run_state_path(),
                )
            except Exception:  # noqa: BLE001 - run state is advisory, never fatal
                pass
            self.manifest = SessionManifest(
                session_id,
                self.settings.manifest_dir,
                target=name,
                config_summary=self._config_summary(),
            )
            self.manifest.set_meta(
                target=name, ra=ra, dec=dec, lp_filter=bool(use_lp_filter)
            )
            # HARDWARE-VERIFIED (2026-07-12): the firmware's ``target_ra_dec``
            # expects RA in HOURS and Dec in degrees. ``ra``/``dec`` arrive in
            # DEGREES (J2000, matching the catalog), so RA must be divided by 15.
            # Passing RA in degrees makes the goto SILENTLY no-op: it returns
            # code 0 but the mount never slews (RA out of range) and drops to
            # ContinuousExposure. This one line broke the whole 2026-07-12 run.
            result = await self.alpaca.method_sync(
                "iscope_start_view",
                {
                    "mode": "star",
                    "target_ra_dec": [ra / 15.0, dec],
                    "target_name": name,
                    "lp_filter": bool(use_lp_filter),
                },
            )
            # A native "Error: ..." result means the scope did NOT start the view
            # (e.g. "Exceeded allotted wait time"). Surface ok:false so the
            # run-session flow won't proceed to solve/stack on a phantom goto.
            if (bad := _native_fail(
                result,
                session_id=session_id,
                target=name,
                ra=ra,
                dec=dec,
                lp_filter=bool(use_lp_filter),
            )) is not None:
                return bad
            return {
                "ok": True,
                "session_id": session_id,
                "target": name,
                "ra": ra,
                "dec": dec,
                "lp_filter": bool(use_lp_filter),
                "result": result,
                "warning": _native_warning(result),
            }
        except AlpacaError as exc:
            return _err(exc)

    async def start_stack(self) -> dict:
        """Start live-stacking exposures (begins capturing/integrating subs)."""
        try:
            # FIRMWARE-DEPENDENT: native start-stack method name.
            result = await self.alpaca.method_sync("iscope_start_stack")
            if (bad := _native_fail(result)) is not None:
                return bad
            return {"ok": True, "result": result, "warning": _native_warning(result)}
        except AlpacaError as exc:
            return _err(exc)

    async def stop_view(self, mode: str = "Stack") -> dict:
        """Stop the current view/stack (``"Stack"`` or ``"ContinuousExposure"``)."""
        try:
            # FIRMWARE-DEPENDENT: native stop method + arg shape.
            result = await self.alpaca.method_sync("iscope_stop_view", [mode])
            if (bad := _native_fail(result, mode=mode)) is not None:
                return bad
            return {
                "ok": True,
                "mode": mode,
                "result": result,
                "warning": _native_warning(result),
            }
        except AlpacaError as exc:
            return _err(exc)

    async def run_autofocus(self) -> dict:
        """Run the autofocus routine; seed the Tier-1 focus baseline afterwards."""
        try:
            result = await self.alpaca.method_sync("start_auto_focus")
            if (bad := _native_fail(result)) is not None:
                return bad
            focus_pos = None
            try:
                focus = await self.alpaca.method_sync(
                    "get_focuser_position", {"ret_obj": True}
                )
                focus_pos = _extract_focus_pos(focus)
                if focus_pos is not None:
                    self.tier1.set_focus_baseline(focus_pos)
            except AlpacaError:
                # Best-effort baseline only; do not fail autofocus on this.
                focus_pos = None
            return {
                "ok": True,
                "result": result,
                "focus_pos": focus_pos,
                "warning": _native_warning(result),
            }
        except AlpacaError as exc:
            return _err(exc)

    async def get_focuser_position(self) -> dict:
        """Read the current focuser position (native ``get_focuser_position``)."""
        try:
            self.provenance.log_call(tool="get_focuser_position", args={})
            focus = await self.alpaca.method_sync(
                "get_focuser_position", {"ret_obj": True}
            )
            if (bad := _native_fail(focus)) is not None:
                return bad
            return {
                "ok": True,
                "focuser": focus,
                "focus_pos": _extract_focus_pos(focus),
                "warning": _native_warning(focus),
            }
        except AlpacaError as exc:
            return _err(exc)

    async def plate_solve(
        self, *, poll_interval_s: float = 2.0, timeout_s: float = 30.0
    ) -> dict:
        """Plate-solve the current field: start a solve, then poll for the result.

        HARDWARE-OBSERVED (fw 8.46, live test 2026-09-24): calling
        ``get_solve_result`` immediately after ``start_solve`` got
        ``{"error": "no solve data", "code": 215}`` — the solve had not
        finished yet — and this used to return a false ``ok: false`` on a
        solve that was still running. Poll ``get_solve_result`` instead, every
        ``poll_interval_s`` (default ~2s) up to ``timeout_s`` (default ~30s);
        this call can take that long to return. Code 215 means "in progress"
        ONLY inside this polling loop — everywhere else a native 215 is an
        ordinary error, like any other nonzero code. ``poll_interval_s`` is
        floored at ``_MIN_POLL_INTERVAL_S`` (0.1s) so the loop is always
        bounded by ``timeout_s`` regardless of what the caller passes —
        ``poll_interval_s=0`` against a device stuck at 215 used to spin
        forever (fix round 1, review finding F1, 2026-09-24). A non-positive
        ``timeout_s`` fails on the first poll, before any sleep.

        On success the additive fields ``ra_deg``/``dec_deg``/``angle_deg``/
        ``fov_deg``/``star_number``/``solve_duration_ms`` sit next to the
        unchanged ``solve_result``, plus ``waited_s`` (how long this call
        actually spent polling). ``ra_deg``/``dec_deg``/``angle_deg`` are the
        *solver's reported position* (RA converted from hours to degrees), not
        the field centre: on fw 8.46 this reported position sits near the
        commanded target even when the object is well off-centre in the frame
        — M1's nebula sat ~23' off-centre in the averaged raw subs (matching
        the live-stack Annotate position) while the solved ``ra_dec`` sat only
        ~4' from the M1 catalog position. For framing, use the stack Annotate
        pixel position (``get_view_state``), not these fields.
        """
        try:
            # FIRMWARE-DEPENDENT: solve method names.
            started = await self.alpaca.method_sync("start_solve")
            # start_solve's reply was discarded, so a rejected solve
            # ({"error": "fail to operate", "code": 207}) fell through to
            # get_solve_result, which returns the PREVIOUS solve: ok:true on a
            # stale solution, under the "never stack on a failed solve" rule
            # (2026-09-22 final review, F1). Any native error fails here —
            # including seestar_alp's "Exceeded allotted wait time" string,
            # which every other command already treats as "did not start".
            if (bad := _native_fail(started)) is not None:
                return bad

            # Fix round 1 (review finding F1, 2026-09-24): waited_s only
            # advanced by poll_interval_s per iteration, so poll_interval_s=0
            # against a device stuck at code 215 spun the loop forever --
            # 0 >= timeout_s never became true. Bound the loop independently
            # of the caller's poll_interval_s by flooring the interval used
            # for both the sleep and the waited_s accumulation; waited_s still
            # reports the actual (floored) time spent, never a lie.
            effective_poll_interval_s = max(poll_interval_s, _MIN_POLL_INTERVAL_S)

            waited_s = 0.0
            result: Any = None
            while True:
                result = await self.alpaca.method_sync("get_solve_result")
                code = _native_solve_code(result)
                if code != _SOLVE_IN_PROGRESS_CODE:
                    break
                if waited_s >= timeout_s:
                    return {
                        "ok": False,
                        "error": (
                            f"plate_solve timed out after {timeout_s:.1f}s "
                            f"waiting for the solve (last code {code})"
                        ),
                        "raw": result,
                        "waited_s": waited_s,
                        "last_code": code,
                    }
                await asyncio.sleep(effective_poll_interval_s)
                waited_s += effective_poll_interval_s

            if (bad := _native_fail(result)) is not None:
                return bad
            return {
                "ok": True,
                "solve_result": result,
                "warning": _native_warning(started) or _native_warning(result),
                "waited_s": waited_s,
                **_extract_solve_fields(result),
            }
        except AlpacaError as exc:
            return _err(exc)

    async def set_filter(self, position: int) -> dict:
        """Set the filter wheel position by index.

        HARDWARE-VERIFIED (Seestar S50, firmware 8.46) — the device reports its
        own mapping via the native ``get_wheel_setting``
        (``{"names": ["dark", "IRCUT", "LP"], ...}``):

        =====  ========  ==================================================
        index  name      use
        =====  ========  ==================================================
        0      dark      shutter closed — dark frames
        1      IRCUT     broadband; filenames carry ``_IRCUT_``
        2      LP        light-pollution filter; filenames carry ``_LP_``
        =====  ========  ==================================================

        Read the current position with ``get_wheel_position`` (returns the bare
        index) and the wheel's busy/idle state with ``get_wheel_state``. Prefer
        reading the mapping from ``get_wheel_setting`` rather than trusting this
        table if a future firmware reorders the wheel.
        """
        try:
            result = await self.alpaca.method_sync("set_wheel_position", [position])
            if (bad := _native_fail(result, position=position)) is not None:
                return bad
            return {
                "ok": True,
                "position": position,
                "result": result,
                "warning": _native_warning(result),
            }
        except AlpacaError as exc:
            return _err(exc)

    async def set_dew_heater(self, on: bool) -> dict:
        """Turn the dew heater on/off.

        Enabling the heater warms the sensor and INVALIDATES any darks built at a
        colder temperature — the caller should rebuild darks / re-run enhancement
        after toggling this.
        """
        try:
            # HARDWARE-VERIFIED (2026-07-12): the dew heater is a power-output
            # channel, NOT a ``set_setting`` key — ``set_setting {"heater":...}``
            # and ``{"heater_enable":...}`` both return "unexpected param". The
            # native method is ``pi_output_set2`` with a {state, value%} object;
            # this flips ``heater_enable`` in get_device_state as confirmed live.
            result = await self.alpaca.method_sync(
                "pi_output_set2",
                {"heater": {"state": bool(on), "value": 90 if on else 0}},
            )
            if (bad := _native_fail(result, heater=bool(on))) is not None:
                return bad
            return {
                "ok": True,
                "heater": bool(on),
                "result": result,
                "warning": _native_warning(result),
                "note": (
                    "Enabling the dew heater changes sensor temperature and "
                    "invalidates existing dark frames; rebuild darks afterwards."
                ),
            }
        except AlpacaError as exc:
            return _err(exc)

    async def park(self) -> dict:
        """Park the telescope (stops tracking and moves the mount to park)."""
        try:
            # FIRMWARE-DEPENDENT: native park method name.
            result = await self.alpaca.method_sync("scope_park")
            if (bad := _native_fail(result)) is not None:
                return bad
            # Park IS wind-down: the run is over, so the state file goes. Leaving
            # it would report a run in progress against a parked mount.
            clear_run_state(self._run_state_path())
            self._run_start_utc = None
            return {"ok": True, "result": result, "warning": _native_warning(result)}
        except AlpacaError as exc:
            return _err(exc)

    async def shutdown(self) -> dict:
        """Power down the Seestar. This TERMINATES the seestar_alp control link.

        After this returns, no further tool calls will reach the device until it
        is physically powered back on and seestar_alp reconnects.
        """
        try:
            result = await self.alpaca.method_sync("pi_shutdown")
            if (bad := _native_fail(result)) is not None:
                return bad
            return {
                "ok": True,
                "result": result,
                "warning": _native_warning(result),
                "note": (
                    "Seestar shutdown issued; this ends the seestar_alp control "
                    "link until the device is powered back on."
                ),
            }
        except AlpacaError as exc:
            return _err(exc)

    # --- data -------------------------------------------------------------

    async def list_subs(self, target: str | None = None) -> dict:
        """List RAW FITS subs the device has saved (optionally one target)."""
        try:
            subs = await self.data.list_subs(target)
            return {
                "ok": True,
                "count": len(subs),
                "subs": [dataclasses.asdict(s) for s in subs],
            }
        except AlpacaError as exc:
            return _err(exc)

    async def download_subs(
        self,
        target: str | None = None,
        names: list[str] | None = None,
        dest: str | None = None,
    ) -> dict:
        """Download RAW subs to local storage (HTTP with SMB fallback).

        Resolves the sub list (optionally one ``target``), optionally filters to
        ``names``, downloads each, and hashes it into the provenance chain.
        """
        try:
            subs = await self.data.list_subs(target)
            if names is not None:
                wanted = set(names)
                subs = [s for s in subs if s.name in wanted]
            results = await self.data.download_subs(subs, dest)
            return {"ok": True, "count": len(results), "downloaded": results}
        except AlpacaError as exc:
            return _err(exc)

    # --- QA ---------------------------------------------------------------

    async def qa_tier1(self) -> dict:
        """Poll firmware telemetry once; return a compact snapshot + health flags.

        Read-only. The bulky raw telemetry is kept in the provenance log but
        trimmed from the returned snapshot to keep tool output small. The flags
        are neutral HEALTH signals, not quality verdicts.
        """
        try:
            snap = await self.tier1.poll()
            snap_dict = dataclasses.asdict(snap)
            # Keep output compact; raw stays in provenance. `snapshot.degraded`
            # survives that strip deliberately: raw carries view_error /
            # device_error, so popping it used to discard the only evidence that
            # a read had failed, leaving an all-null snapshot with empty flags
            # that reads as "polled fine, nothing wrong".
            snap_dict.pop("raw", None)
            degraded = bool(snap_dict.get("degraded"))
            return {
                "ok": True,
                "degraded": degraded,
                "snapshot": snap_dict,
                "flags": self.tier1.check(),
                "status_line": (
                    "telemetry unavailable — scope unreachable or not responding"
                    if degraded
                    else self.tier1.status_line()
                ),
                "trends": self.tier1.trends(),
            }
        except AlpacaError as exc:
            return _err(exc)

    def _resolve_paths(
        self, target: str | None, paths: list[str] | None
    ) -> list[Path]:
        """Resolve explicit ``paths`` else glob ``data_dir`` for target FITS."""
        if paths is not None:
            return [Path(p) for p in paths]
        data_dir = Path(self.settings.data_dir)
        if not data_dir.exists():
            return []
        matches: list[Path] = []
        for pattern in ("*.fit", "*.fits"):
            for p in sorted(data_dir.glob(pattern)):
                if target is None or target.lower() in p.name.lower():
                    matches.append(p)
        return matches

    @staticmethod
    def _compact_report(report: Any) -> dict:
        """Summarize a SessionReport: session aggregates + per-sub verdicts+metrics.

        Per-sub metrics are included deliberately. Consumers chart the session's
        *distribution* (FWHM / eccentricity / SNR / star count across subs), which
        the ``medians`` aggregate cannot reconstruct, and the arrays are computed
        for the verdicts anyway — dropping them here discarded the only copy.
        ``name`` is the stable per-sub key. It is the filename **stem** — no
        extension — so a join key built as ``f"{name}.fit"`` matches nothing.

        Every metric is nullable: a sub that could not be analyzed still appears,
        with ``metrics.error`` set. Values are finite-or-None by construction
        (``qa_tier2._finite`` guards them), so this stays strict-JSON safe.
        """
        return {
            "target": report.target,
            "total": report.total,
            "kept": report.kept,
            # Aggregates are rounded to the same precision as the per-sub metrics.
            # They are derived from the same measurements, so emitting 16 digits
            # here while rounding the values they summarise implied a precision
            # difference that does not exist. Rounding happens at the WIRE
            # boundary only — classification upstream still uses full precision.
            "wfwhm": _round_metric(report.wfwhm),
            "medians": {k: _round_metric(v) for k, v in (report.medians or {}).items()},
            # The cutoffs this session was actually scored against, as values.
            # They appeared only inside `reasons` prose, so a consumer wanting to
            # draw a cutoff line had to parse a sentence — which is re-deriving a
            # verdict. Session-relative ones move night to night.
            "thresholds": {
                k: _round_metric(v)
                for k, v in (getattr(report, "thresholds", None) or {}).items()
            },
            "dominant_reject_cause": report.dominant_reject_cause,
            "subs": [
                {
                    "name": v.name,
                    "verdict": v.verdict,
                    "reasons": v.reasons,
                    "metrics": _compact_metrics(v.metrics),
                }
                for v in report.subs
            ],
        }

    async def qa_tier2(
        self, target: str | None = None, paths: list[str] | None = None
    ) -> dict:
        """Score RAW subs (photutils FWHM/ecc/SNR/stars) into PASS/MARGINAL/REJECT.

        Read-only analysis. Returns a per-sub verdict summary + keep-list. Each
        sub carries its ``metrics`` (see :meth:`_compact_report`) so a consumer can
        chart the session's distribution, and ``summary.thresholds`` carries the
        cutoffs those verdicts were made against.
        """
        try:
            resolved = self._resolve_paths(target, paths)
            report = analyze_session(
                resolved, self.settings, target=target, provenance=self.provenance
            )
            return {
                "ok": True,
                "summary": self._compact_report(report),
                "keep_list": report.keep_list,
            }
        except AlpacaError as exc:
            return _err(exc)

    async def qa_session_report(
        self, target: str | None = None, paths: list[str] | None = None
    ) -> dict:
        """Full session wind-down: score subs, write JSON+MD report + manifest.

        Like ``qa_tier2`` but also records every verdict into the session manifest
        (creating a fallback manifest if no session is open), writes a Markdown +
        JSON QA report, and writes the manifest. Returns the artifact paths.
        """
        try:
            resolved = self._resolve_paths(target, paths)
            eff_target = target or self.target
            manifest = self.manifest
            if manifest is None:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                sid = self.session_id or f"{_slug(eff_target or 'session')}-{stamp}"
                self.session_id = sid
                manifest = SessionManifest(
                    sid,
                    self.settings.manifest_dir,
                    target=eff_target,
                    config_summary=self._config_summary(),
                )
                self.manifest = manifest

            report = analyze_session(
                resolved,
                self.settings,
                target=eff_target,
                provenance=self.provenance,
                manifest=manifest,
            )

            reports_dir = Path(self.settings.data_dir) / "reports"
            stem = f"qa_report_{self.session_id}"
            md_path, json_path = write_report(report, reports_dir, stem=stem)
            manifest_path = manifest.write()

            return {
                "ok": True,
                "summary": self._compact_report(report),
                "keep_list": report.keep_list,
                "report_json": str(json_path),
                "report_md": str(md_path),
                "manifest": str(manifest_path),
            }
        except AlpacaError as exc:
            return _err(exc)

    # --- planning ---------------------------------------------------------

    def _site_path(self) -> Path:
        """Path of the persisted site profile under the configured data dir."""
        return self.settings.data_dir / "site_profile.json"

    def _projects_path(self) -> Path:
        """Path of the persisted projects/history store under the data dir."""
        return self.settings.data_dir / "projects.json"

    def _run_state_path(self) -> Path:
        """Path of the live-run state file under the data dir."""
        return self.settings.data_dir / "run_state.json"

    async def get_run_state(self) -> dict:
        """Is a run in progress right now, and what is it doing?

        Read-only. Returns ``state`` of ``"active"``, ``"idle"`` or ``"unknown"``
        — tri-valued rather than a boolean, because a stale file must not be
        readable as a confident "yes". ``unknown`` means a run was written but
        its stamp is too old to vouch for (the writer probably died), which is
        different from ``idle`` and must not be collapsed into it.

        **``run`` is NOT a liveness signal.** It carries the record in TWO states:
        ``active``, and ``unknown`` when the stamp went stale — the stale record is
        retained deliberately, because "here is what was running when we lost
        track" is more useful than discarding it. It is ``None`` only for ``idle``
        and for an unreadable file. So ``run is not None`` does **not** mean a
        session is live; only ``state == "active"`` means that. Branch on
        ``state``, never on the presence of ``run``.
        """
        try:
            self.provenance.log_call(tool="get_run_state", args={})
            return {"ok": True, **read_run_state(self._run_state_path())}
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    def _sky_log_path(self) -> Path:
        """Path of the local weather-gated sky-failure histogram under the data dir."""
        return self.settings.data_dir / "sky_failures.json"

    async def _current_gps(self) -> tuple[float, float] | None:
        """Best-effort scope GPS ``(lat, lon)`` from ``get_device_state``; None if
        unavailable.

        This is I/O at the tool layer (like the weather read) — the planning cores
        stay pure/deterministic. Any device fault or an empty/unparseable state
        resolves to ``None`` (GPS unknown → fail-safe: assume the saved site).
        """
        try:
            dev = await self.alpaca.method_sync("get_device_state")
            return _parse_gps(dev)
        except Exception:  # noqa: BLE001 - any device fault → GPS unknown
            return None

    async def _location_block(self, site: SiteProfile) -> dict:
        """Reconcile the scope's live GPS against the saved site and DISCLOSE any
        mismatch, so a stale horizon mask is never silently applied at a new site.

        Returns ``{"matched", "distance_km", "site_name", "mask_applied",
        "warning"}``:

        * GPS unknown (``_current_gps`` None) → ``matched=None``,
          ``mask_applied=True`` (assume the saved site) + an "unverified" note.
        * within ``location_tolerance_km`` → ``matched=True``, ``mask_applied=True``.
        * beyond tolerance → ``matched=False``, ``mask_applied=False`` + a warning
          that the mask was NOT applied and a new profile should be set/confirmed.
        """
        gps = await self._current_gps()
        if gps is None:
            return {
                "matched": None,
                "distance_km": None,
                "site_name": site.name,
                "mask_applied": True,
                "warning": f"GPS unverified — assuming saved site '{site.name}'.",
            }
        ok, dist = location_status(site, gps[0], gps[1])
        if ok:
            return {
                "matched": True,
                "distance_km": round(dist, 1),
                "site_name": site.name,
                "mask_applied": True,
                "warning": None,
            }
        return {
            "matched": False,
            "distance_km": round(dist, 1),
            "site_name": site.name,
            "mask_applied": False,
            "warning": (
                f"Scope is ~{dist:.0f} km from saved site '{site.name}' — horizon "
                "mask NOT applied. Set/confirm a profile for this location."
            ),
        }

    async def get_site_profile(self) -> dict:
        """Return the persisted observing-site profile, if one has been set.

        Read-only. Returns ``ok:false`` (not an error) when no profile exists so
        the caller can prompt for one via ``set_site_profile``.
        """
        try:
            self.provenance.log_call(tool="get_site_profile", args={})
            profile = load_site(self._site_path())
            if profile is None:
                return {"ok": False, "error": "no site profile set — use set_site_profile"}
            return {"ok": True, "profile": dataclasses.asdict(profile)}
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    async def set_site_profile(
        self,
        name: str,
        lat: float,
        lon: float,
        elevation_m: float = 0.0,
        bortle: int | None = None,
        sqm: float | None = None,
        horizon_mask: list[list[float]] | None = None,
        min_altitude_deg: float = 20.0,
        field_rotation_ceiling_deg: float = 60.0,
    ) -> dict:
        """Persist the observing-site profile used by every planning tool.

        Records position, sky-darkness (Bortle/SQM), a horizon mask
        (``[[az_min, az_max, alt_min], ...]``) and the usable-altitude band.
        Writes JSON under the data dir; no device motion.
        """
        try:
            self.provenance.log_call(
                tool="set_site_profile",
                args={
                    "name": name,
                    "lat": lat,
                    "lon": lon,
                    "elevation_m": elevation_m,
                    "bortle": bortle,
                    "sqm": sqm,
                    "min_altitude_deg": min_altitude_deg,
                    "field_rotation_ceiling_deg": field_rotation_ceiling_deg,
                },
            )
            mask = [tuple(arc) for arc in (horizon_mask or [])]
            profile = SiteProfile(
                name=name,
                lat_deg=lat,
                lon_deg=lon,
                elevation_m=elevation_m,
                bortle=bortle,
                sqm=sqm,
                horizon_mask=mask,
                min_altitude_deg=min_altitude_deg,
                field_rotation_ceiling_deg=field_rotation_ceiling_deg,
            )
            save_site(profile, self._site_path())
            return {"ok": True, "profile": dataclasses.asdict(profile)}
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    async def assess_conditions(self, date: str | None = None) -> dict:
        """Go/no-go sky verdict for tonight: weather + moon over the dark window.

        Reads the clock only to resolve "tonight" when ``date`` is omitted. A
        bare site-local date (``YYYY-MM-DD``) means the night beginning on that
        date's evening; omitted means tonight (or the current night if already
        dark) — see :func:`planning_when`. A weather outage degrades to ``go=None``
        (non-fatal); planning still runs.
        """
        from datetime import datetime, timezone

        try:
            self.provenance.log_call(tool="assess_conditions", args={"date": date})
            site = load_site(self._site_path())
            if site is None:
                return {"ok": False, "error": "no site profile set"}
            # Resolved against the site: `date or now` fed straight to dark_window
            # planned LAST night from a morning call, and a bare date the night
            # before it (2026-09-22 review).
            when = planning_when(site, date or datetime.now(timezone.utc).isoformat())
            block = await self._location_block(site)
            site_for_engine = (
                site
                if block["mask_applied"]
                else dataclasses.replace(site, horizon_mask=[])
            )
            window = dark_window(site_for_engine, when)
            illum = moon_illumination(when)
            assessment = await self._weather_cached(site_for_engine, window, illum)
            return {"ok": True, "location": block, **dataclasses.asdict(assessment)}
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    async def get_target_observability(
        self, target: str, date: str | None = None
    ) -> dict:
        """Observability of one named DSO tonight (altitude, sweet band, moon).

        Reads the clock only to resolve "tonight" when ``date`` is omitted. A
        bare site-local date (``YYYY-MM-DD``) means the night beginning on that
        date's evening; omitted means tonight (or the current night if already
        dark) — see :func:`planning_when`. Read-only; no device motion.

        The result's ``now`` block is about the target's position at the REAL
        current instant, independent of ``date``: ``date`` only selects which
        night ``observability``/``dark_window_utc`` describe (live test
        2026-09-24, Task 4 — every heartbeat needed a scratch astropy script to
        get the current altitude/azimuth against the floor and ceiling).
        """
        # No local `from datetime import ...` here (unlike this method's
        # siblings): `datetime`/`timezone` stay the module-level names imported
        # at the top of this file so tests can monkeypatch `datetime` on this
        # module directly for the `now` block below, without touching the
        # stdlib `datetime` module (which astropy also reads internally).
        try:
            self.provenance.log_call(
                tool="get_target_observability",
                args={"target": target, "date": date},
            )
            site = load_site(self._site_path())
            if site is None:
                return {"ok": False, "error": "no site profile set"}
            when = planning_when(site, date or datetime.now(timezone.utc).isoformat())
            t = find_target(target)
            if t is None:
                return {"ok": False, "error": f"unknown target: {target}"}
            # Named as a field, not just prose (2026-09-22 review, Task 7): the
            # caller cannot otherwise confirm which night this observability was
            # computed over, now that `date=None` re-anchors to the upcoming
            # night. Computed once and handed to observability() below instead
            # of letting it recompute the same window.
            window = dark_window(site, when)
            obs = observability(site, t, when, dark_window_utc=window)
            # `now`: the REAL current instant, always (never `when`, which
            # tracks `date`) — this is the only place in the method the clock
            # is read a second time. Mirrors the exact above_floor/sweet-band
            # formula `_observability` uses per-sample in astro.py, so `now`
            # agrees with the rest of the result (live test 2026-09-24, Task 4).
            now_iso = datetime.now(timezone.utc).isoformat()
            now_az, now_alt = azalt_at(site, t, now_iso)
            now_unblocked = not is_blocked(site, now_az, now_alt)
            now_above_floor = now_alt >= site.min_altitude_deg and now_unblocked
            now_in_sweet_band = (
                now_alt >= site.min_altitude_deg
                and now_alt <= site.field_rotation_ceiling_deg
                and now_unblocked
            )
            return {
                "ok": True,
                "target": dataclasses.asdict(t),
                "observability": dataclasses.asdict(obs),
                "dark_window_utc": window,
                "now": {
                    "utc": now_iso,
                    "alt_deg": now_alt,
                    "az_deg": now_az,
                    "above_floor": now_above_floor,
                    "in_sweet_band": now_in_sweet_band,
                },
            }
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    async def plan_targets(
        self,
        date: str | None = None,
        types: list[str] | None = None,
        min_alt: float | None = None,
        limit: int = 10,
        avoid_recent_days: int = 2,
        prefer_projects: bool = True,
    ) -> dict:
        """Rank tonight's best DSO targets — a scored, reasoned shortlist.

        Reads the clock only to resolve "tonight" when ``date`` is omitted. A
        bare site-local date (``YYYY-MM-DD``) means the night beginning on that
        date's evening; omitted means tonight (or the current night if already
        dark) — see :func:`planning_when`. Returns a compact per-target summary
        (id/name/type/score/reasons/window + key observability numbers) rather
        than the full nested record.

        When ``prefer_projects`` (default), the persisted projects/history store
        is loaded and threaded into the ranker so active projects still short of
        their goal are boosted and targets imaged within ``avoid_recent_days``
        are suppressed. Set ``prefer_projects=False`` for pure Phase-1 ranking.
        """
        from datetime import datetime, timezone

        try:
            self.provenance.log_call(
                tool="plan_targets",
                args={
                    "date": date,
                    "types": types,
                    "min_alt": min_alt,
                    "limit": limit,
                    "avoid_recent_days": avoid_recent_days,
                    "prefer_projects": prefer_projects,
                },
            )
            site = load_site(self._site_path())
            if site is None:
                return {"ok": False, "error": "no site profile set"}
            # Idempotent, so simulate_night handing us its resolved instant as
            # `date` ranks the same night it schedules.
            when = planning_when(site, date or datetime.now(timezone.utc).isoformat())
            # GPS reconcile: if the scope has moved off the saved site, disclose it
            # and run the astronomy against a mask-stripped copy (keep the altitude
            # floor; drop the stale obstruction arcs) so blocked sky is not dropped.
            block = await self._location_block(site)
            site_for_engine = (
                site
                if block["mask_applied"]
                else dataclasses.replace(site, horizon_mask=[])
            )
            illum = moon_illumination(when)
            window = dark_window(site_for_engine, when)
            conditions = await self._weather_cached(site_for_engine, window, illum)
            projects = (
                load_projects(self._projects_path()) if prefer_projects else None
            )
            # Same window already computed above for the weather assessment
            # (site_for_engine only strips the horizon mask — dark_window
            # depends solely on lat/lon/elevation, so it is identical to
            # site's) — hand it to rank_targets instead of letting it, or the
            # up-to-120 catalog targets under it, recompute it (2026-09-22
            # review, Task 7: that took 120x `observability` from 8.1s to
            # 11.1s once Task 6 widened dark_window's Sun grid).
            plans = rank_targets(
                site_for_engine,
                when,
                load_catalog(),
                conditions,
                types=types,
                min_alt=min_alt,
                limit=limit,
                dark_window_utc=window,
                projects=projects,
                now_utc=when,
                recent_days=avoid_recent_days,
            )
            return {
                "ok": True,
                "location": block,
                "dark_window_utc": window,
                "conditions": {
                    "go": conditions.go,
                    "suitability": conditions.suitability,
                    "source": conditions.source,
                },
                "count": len(plans),
                "targets": [
                    {
                        "id": p.target.id,
                        "name": p.target.name,
                        "type": p.target.type,
                        "score": p.score,
                        "reasons": p.reasons,
                        "best_window_utc": p.best_window_utc,
                        "recommended_subs": p.recommended_subs,
                        "recommended_exposure_s": p.recommended_exposure_s,
                        "framing_note": p.framing_note,
                        "max_alt_deg": round(p.observability.max_alt_deg, 1),
                        "transit_utc": p.observability.transit_utc,
                        "sweet_band_min": round(
                            p.observability.dark_minutes_in_sweet_band
                        ),
                        "moon_sep_deg": round(p.observability.moon_sep_deg, 1),
                    }
                    for p in plans
                ],
            }
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    # --- learned horizon mask (obstruction inference) ---------------------

    async def log_sky_result(
        self,
        target: str | None = None,
        az: float | None = None,
        alt: float | None = None,
        solved: bool = True,
        weather_go: bool | None = None,
    ) -> dict:
        """Record one plate-solve outcome into the weather-gated obstruction log.

        The pointing is taken from explicit ``az``/``alt`` when given, else derived
        from ``target`` (catalog + saved site) at *now*. Bad-weather failures are
        excluded from obstruction inference (see :func:`record_sky_result`). Reads
        the clock only for the record's timestamp. Writes the local sky-failure
        histogram; no device motion.
        """
        try:
            self.provenance.log_call(
                tool="log_sky_result",
                args={
                    "target": target,
                    "az": az,
                    "alt": alt,
                    "solved": solved,
                    "weather_go": weather_go,
                },
            )
            now = datetime.now(timezone.utc).isoformat()
            site = load_site(self._site_path())
            if site is None:
                return {"ok": False, "error": "no site profile set"}

            if az is None or alt is None:
                if not target:
                    return {
                        "ok": False,
                        "error": "need target+site or explicit az/alt",
                    }
                t = find_target(target)
                if t is None:
                    return {"ok": False, "error": f"unknown target: {target}"}
                az, alt = azalt_at(site, t, now)

            if weather_go is None:
                # Best-effort weather read so a bad-sky failure is not mislearnt as
                # an obstruction; an outage leaves weather_go None (counts as ok).
                try:
                    weather_go = (
                        await self._weather_cached(
                            site,
                            dark_window(site, now),
                            0.0,
                        )
                    ).go
                except Exception:  # noqa: BLE001 - weather outage is non-fatal
                    weather_go = None

            weather_ok = weather_go is not False
            record_sky_result(
                az,
                alt,
                ok=bool(solved),
                weather_ok=weather_ok,
                now_utc=now,
                lat=site.lat_deg,
                lon=site.lon_deg,
                path=self._sky_log_path(),
            )
            return {
                "ok": True,
                "az": round(az, 1),
                "alt": round(alt, 1),
                "solved": bool(solved),
                "weather_ok": weather_ok,
            }
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    async def suggest_horizon_mask(self) -> dict:
        """Suggest horizon-mask arcs learned from cross-night, weather-gated fails.

        READ-ONLY: inference only — it never edits the saved mask (use
        ``add_horizon_mask`` to accept a suggestion). Location-scoped to the saved
        site so obstructions learned elsewhere never surface here.
        """
        try:
            self.provenance.log_call(tool="suggest_horizon_mask", args={})
            site = load_site(self._site_path())
            if site is None:
                return {"ok": False, "error": "no site profile set"}
            cands = suggest_obstructions(
                self._sky_log_path(),
                cur_lat=site.lat_deg,
                cur_lon=site.lon_deg,
                location_tolerance_km=getattr(site, "location_tolerance_km", 1.0),
            )
            return {
                "ok": True,
                "candidates": [dataclasses.asdict(c) for c in cands],
                "count": len(cands),
            }
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    async def add_horizon_mask(
        self, az_min: float, az_max: float, alt_min: float
    ) -> dict:
        """Append one horizon-mask arc to the saved site profile (user confirm step).

        SIDE EFFECT: persists an added ``(az_min, az_max, alt_min)`` arc to the
        site profile — the ONLY path that edits the mask, always by explicit user
        action (suggestions never auto-apply).
        """
        try:
            self.provenance.log_call(
                tool="add_horizon_mask",
                args={"az_min": az_min, "az_max": az_max, "alt_min": alt_min},
            )
            site = load_site(self._site_path())
            if site is None:
                return {"ok": False, "error": "no site profile set"}
            site.horizon_mask.append((float(az_min), float(az_max), float(alt_min)))
            save_site(site, self._site_path())
            return {"ok": True, "profile": dataclasses.asdict(site)}
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    # --- autonomous night -------------------------------------------------

    async def simulate_night(
        self,
        date: str | None = None,
        types: list[str] | None = None,
        limit: int | None = None,
    ) -> dict:
        """Dry-run tonight's autonomous plan as an ordered target schedule.

        Reads/computes only — issues NO device motion. Ranks tonight's targets
        via :meth:`plan_targets`, then rotates them through the dark window with
        the pure :func:`plan_night` sequencer (45-min slot cap). Reads the clock
        only to resolve "tonight" when ``date`` is omitted. A bare site-local
        date (``YYYY-MM-DD``) means the night beginning on that date's evening;
        omitted means tonight (or the current night if already dark) — see
        :func:`planning_when`.
        """
        try:
            self.provenance.log_call(
                tool="simulate_night",
                args={"date": date, "types": types, "limit": limit},
            )
            site = load_site(self._site_path())
            if site is None:
                return {"ok": False, "error": "no site profile set"}
            when = planning_when(site, date or datetime.now(timezone.utc).isoformat())
            plan = await self.plan_targets(date=when, types=types, limit=limit)
            if not plan.get("ok"):
                return plan
            dark = dark_window(site, when)
            # Cap each slot (45 min) so the night ROTATES through the ranked list
            # instead of handing one target the whole window (2026-07-12 fix).
            sched = plan_night(plan["targets"], dark, max_slot_min=45.0)
            return {
                "ok": True,
                "conditions": plan.get("conditions"),
                # Re-surface the GPS/location reconcile computed by plan_targets so
                # a stale-mask disclosure rides along on the dry-run schedule too.
                "location": plan.get("location"),
                "dark_window_utc": dark,
                "schedule": [dataclasses.asdict(s) for s in sched],
                "projected_targets": len(sched),
            }
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    async def check_night_guardrails(
        self,
        session_start_utc: str,
        max_session_hours: float = 10.0,
        battery_floor_pct: float = 20.0,
        dawn_margin_min: float = 15.0,
    ) -> dict:
        """Evaluate the hard-stop safety conditions for one autonomous iteration.

        Gathers live device health and weather best-effort, then hands them to
        the pure :func:`evaluate_guardrails`. If device health cannot be
        confirmed (no ``get_device_state``), it fails SAFE — ``connected=False``
        forces a ``park_and_stop`` verdict. Reads the clock only for "now".
        """
        try:
            self.provenance.log_call(
                tool="check_night_guardrails",
                args={
                    "session_start_utc": session_start_utc,
                    "max_session_hours": max_session_hours,
                    "battery_floor_pct": battery_floor_pct,
                    "dawn_margin_min": dawn_margin_min,
                },
            )
            now = datetime.now(timezone.utc).isoformat()
            site = load_site(self._site_path())
            if site is None:
                return {"ok": False, "error": "no site profile set"}
            dark = dark_window(site, now)

            # Live device health — fail SAFE to (disconnected, unverified,
            # unknown) on ANY failure so a lost link parks the run. Identity comes
            # from get_device_state; battery from pi_get_info (two native calls —
            # battery is NOT in get_device_state).
            battery: float | None = None
            try:
                dev = await self.alpaca.method_sync("get_device_state")
                connected, verified = _parse_device_health(dev)
                # Battery comes from the SAME reply — see _parse_battery. A second
                # native round-trip for it was pure waste on a link that starves
                # under load, and guardrails now run inside each slot, not just at
                # target boundaries.
                battery = _parse_battery(dev)
            except Exception:  # noqa: BLE001 - any device fault → fail safe
                connected, verified = (False, False)

            # Weather is best-effort and non-fatal: an outage → unknown, which
            # the guardrail core treats as observability-only, not a hard stop.
            try:
                # Cached: this is the call that burned ~8M meteoblue credits on
                # 2026-07-31 when a dashboard polled it once a minute for 11
                # hours. The guardrail uses only `.go` from the whole forecast.
                weather_go = (await self._weather_cached(site, dark, 0.0)).go
            except Exception:  # noqa: BLE001 - weather outage is non-fatal
                weather_go = None

            verdict = evaluate_guardrails(
                now_utc=now,
                dark_window_utc=dark,
                session_start_utc=session_start_utc,
                battery_pct=battery,
                weather_go=weather_go,
                connected=connected,
                verified=verified,
                max_session_hours=max_session_hours,
                battery_floor_pct=battery_floor_pct,
                dawn_margin_min=dawn_margin_min,
            )
            return {"ok": True, **dataclasses.asdict(verdict)}
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    # --- projects ---------------------------------------------------------

    async def list_projects(self, detail: str = "summary") -> dict:
        """Return every persisted project (goals + accumulated integration).

        Read-only. Degrades to an empty list when no store exists yet.

        ``detail="summary"`` (the default) **omits the ``sessions`` key entirely**
        rather than returning an empty list. The full history grows without bound
        — a mature store costs thousands of tokens per call — but an empty list
        would parse cleanly and silently render an empty history table, which is a
        worse failure than a loud one. Omission makes a consumer that needs the
        history fail immediately and obviously. Pass ``detail="full"`` for the
        complete records; that shape is byte-identical to the historical payload.
        """
        try:
            self.provenance.log_call(tool="list_projects", args={"detail": detail})
            projects = load_projects(self._projects_path())
            return {
                "ok": True,
                "projects": [
                    _project_payload(p, detail) for p in projects.values()
                ],
                "count": len(projects),
            }
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    async def get_project(self, target: str) -> dict:
        """Return one project's goal, progress and session history by target id.

        Read-only. Returns ``ok:false`` (not an error) when no project exists.
        """
        try:
            self.provenance.log_call(tool="get_project", args={"target": target})
            proj = _get_project(target, path=self._projects_path())
            if proj is None:
                return {"ok": False, "error": f"no project for {target}"}
            return {"ok": True, "project": dataclasses.asdict(proj)}
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    async def set_project_goal(self, target: str, goal_minutes: float) -> dict:
        """Create/update a target's integration goal (minutes) for the planner.

        Persists to the local projects store. Resolves a display name from the
        catalog when possible. Does not touch accumulated integration or history.
        """
        try:
            self.provenance.log_call(
                tool="set_project_goal",
                args={"target": target, "goal_minutes": goal_minutes},
            )
            now = datetime.now(timezone.utc).isoformat()
            t = find_target(target)
            name = t.name if t is not None else target
            proj = upsert_project(
                target,
                name,
                goal_minutes=goal_minutes,
                now_utc=now,
                path=self._projects_path(),
            )
            return {"ok": True, "project": dataclasses.asdict(proj)}
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    async def log_session_result(
        self,
        target: str,
        integration_minutes: float,
        subs_total: int,
        subs_kept: int,
        median_fwhm: float | None = None,
        notes: str = "",
    ) -> dict:
        """Record a finished imaging session into a target's project history.

        Appends a session, accumulates kept integration toward the goal, and
        auto-completes the project when the goal is met. Creates the project if
        it does not yet exist.

        When ``median_fwhm`` is omitted it is backfilled from the newest QA report
        for the target (see :func:`_latest_median_fwhm`), so a session logged at
        wind-down carries its sharpness figure without the caller re-typing it. An
        explicitly supplied value always wins; if no report exists the field stays
        ``None``.
        """
        try:
            if median_fwhm is None:
                median_fwhm = _latest_median_fwhm(self.settings.data_dir, target)
            self.provenance.log_call(
                tool="log_session_result",
                args={
                    "target": target,
                    "integration_minutes": integration_minutes,
                    "subs_total": subs_total,
                    "subs_kept": subs_kept,
                    "median_fwhm": median_fwhm,
                    "notes": notes,
                },
            )
            now = datetime.now(timezone.utc).isoformat()
            t = find_target(target)
            name = t.name if t is not None else target
            proj = _log_session_result(
                target,
                name,
                integration_minutes=integration_minutes,
                subs_total=subs_total,
                subs_kept=subs_kept,
                median_fwhm=median_fwhm,
                notes=notes,
                now_utc=now,
                path=self._projects_path(),
            )
            return {"ok": True, "project": dataclasses.asdict(proj)}
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    async def recommend_projects(
        self, limit: int | None = None, detail: str = "summary"
    ) -> dict:
        """Active projects still needing data, most-needed first.

        Read-only. Answers "what should I image more of?" for the planner.

        ``detail`` behaves exactly as on :meth:`list_projects` — summary omits the
        ``sessions`` key. The parameter exists here even though the default is the
        same, so a consumer has an escape hatch on both tools rather than one; a
        default change with no way to opt out is a break with no remedy.
        """
        try:
            self.provenance.log_call(
                tool="recommend_projects", args={"limit": limit, "detail": detail}
            )
            projects = _recommend_projects(path=self._projects_path(), limit=limit)
            return {
                "ok": True,
                "projects": [_project_payload(p, detail) for p in projects],
                "count": len(projects),
            }
        except Exception as exc:  # noqa: BLE001 - tool-facing never-raise contract
            return {"ok": False, "error": str(exc)}

    # --- lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        """Close underlying async clients."""
        await self.alpaca.aclose()
        close = getattr(self.data, "aclose", None)
        if close is not None:
            await self.data.aclose()


#: Decimal places kept for reported per-sub metrics. These are measurements good
#: to ~3 significant figures (FWHM/HFR in pixels, eccentricity 0..1, an SNR proxy);
#: emitting full float repr implies precision that does not exist and inflates a
#: 1400-sub payload by roughly a third for no information.
_METRIC_DP = 4


def _round_metric(value: Any) -> Any:
    """Round a float to :data:`_METRIC_DP`; pass anything else through unchanged."""
    return round(value, _METRIC_DP) if isinstance(value, float) else value


def _compact_metrics(metrics: Any) -> dict:
    """Per-sub metrics for the wire: rounded, without the duplicated name.

    ``SubMetrics.name`` repeats the sub key already carried one level up, so it is
    dropped here. Floats are finite-or-None by construction (``qa_tier2._finite``),
    so rounding cannot introduce a NaN/Infinity token — this stays strict-JSON safe.
    """
    out: dict[str, Any] = {}
    for key, value in dataclasses.asdict(metrics).items():
        if key == "name":
            continue  # already the sub's key
        out[key] = round(value, _METRIC_DP) if isinstance(value, float) else value
    return out


def _project_payload(project: Any, detail: str) -> dict:
    """One project for the wire, at the requested level of detail.

    ``detail="full"`` reproduces the historical payload exactly, including the
    complete ``sessions`` list. ``detail="summary"`` **removes the ``sessions``
    key** and adds ``sessions_count`` plus ``last_session_utc`` in its place.

    Removing the key rather than emptying it is deliberate: a consumer that needs
    the history then fails to parse and says so, instead of parsing happily and
    rendering an empty table. An empty list is indistinguishable from "this
    project has no sessions", which is a real and different state.
    """
    data = dataclasses.asdict(project)
    if detail == "full":
        return data
    sessions = data.pop("sessions", []) or []
    data["sessions_count"] = len(sessions)
    # max(), not sessions[-1]: now_utc is caller-supplied and nothing sorts the
    # list on load, so a backfilled session appended after the fact would sit last
    # while carrying an older date — and report that older date as "last".
    data["last_session_utc"] = (
        max(s["date_utc"] for s in sessions) if sessions else None
    )
    return data


def _latest_median_fwhm(data_dir: Path | str, target: str) -> float | None:
    """Median FWHM from the newest QA report for ``target``; ``None`` if unknown.

    ``log_session_result`` takes ``median_fwhm`` as an optional argument, so in
    practice it was omitted every time and every session record stored ``None``:
    the value only exists once subs have been downloaded and scored at wind-down,
    and carrying it back by hand is exactly the step that gets skipped. Reports are
    written as ``reports/qa_report_<slug>-<timestamp>.json``, whose names sort
    chronologically, so the last match is the most recent session for that target.

    Never raises — a missing directory, unreadable file, or malformed report all
    degrade to ``None``, which is the same "unknown" the caller would have passed.
    """
    try:
        reports = sorted(
            (Path(data_dir) / "reports").glob(f"qa_report_{_slug(target)}-*.json")
        )
        if not reports:
            return None
        data = json.loads(reports[-1].read_text(encoding="utf-8"))
        value = (data.get("medians") or {}).get("fwhm")
        return float(value) if isinstance(value, (int, float)) else None
    except Exception:  # noqa: BLE001 - best-effort backfill, never fatal
        return None


def _err(exc: AlpacaError) -> dict:
    """Uniform error envelope for a caught :class:`AlpacaError`."""
    return {
        "ok": False,
        "error": str(exc),
        "error_number": getattr(exc, "error_number", None),
    }


def _native_error_parts(value: dict) -> tuple[str, Any] | None:
    """Return ``(text, code)`` for a dict's truthy ``"error"`` key, else ``None``.

    Shared by :func:`_native_error` and :func:`_native_warning` so the two never
    disagree on which code wins: a nested JSON-RPC error object's own ``"code"``
    overrides the envelope's top-level one.
    """
    err = value.get("error")
    if not err:
        return None
    code = value.get("code")
    if isinstance(err, dict):
        code = err.get("code", code)
        text = str(err.get("message") or err)
    else:
        text = str(err)
    return text, code


def _is_native_success_code(code: Any) -> bool:
    """``True`` only for an explicit integer ``0`` — never a missing code."""
    return code == 0 and not isinstance(code, bool)


def _native_error(value: Any) -> str | None:
    """Return an error string if a native action result signals failure, else None.

    seestar_alp tunnels native JSON-RPC results verbatim inside an otherwise-ok
    Alpaca envelope. When the device is idle/slow it can return a result *string*
    like ``"Error: Exceeded allotted wait time for result"`` even though the
    Alpaca ``ErrorNumber`` is 0. Detect that so the controller surfaces it as
    ``ok:false`` instead of a false ``ok:true``. Handles both a bare string and a
    dict whose ``"result"`` is such a string.

    HARDWARE-OBSERVED (fw 8.46, 2026-08-03): the firmware itself rejects a
    command with a JSON-RPC dict, ``{"error": "method not found", "code": 103}``
    (the probe recorded in ``data_client.py``). Until 2026-09-22 that shape
    passed as success, so park/goto/stack/filter/heater/shutdown returned
    ``ok:true`` on a command the scope never ran — and ``park`` then cleared the
    run state although the mount never folded. Any dict with a truthy
    ``"error"`` is now an error, reported as ``"<text> (code <n>)"``. A normal
    reply carries ``"code": 0`` and no ``"error"``, so it is not affected. A
    standard JSON-RPC 2.0 error object (``{"message", "code"}``) is read too.

    HARDWARE-OBSERVED (fw 8.46, live test 2026-09-24): the dew heater's native
    ``pi_output_set2`` answered every call with ``{"jsonrpc": "2.0", "Timestamp":
    "663.578618899", "method": "pi_output_set2", "error": "expected object
    param", "code": 0, "result": 0, "id": 10104}`` while it DID apply the
    change — confirmed by switching the heater off/on and reading
    ``get_device_state``'s ``result.setting.heater_enable`` flip each time. A
    truthy ``"error"`` is now a failure ONLY when ``code`` is present and
    nonzero, or absent; an explicit ``code == 0`` is success regardless of the
    ``"error"`` text, so a missing code is never read as proof of success. The
    firmware's odd text on a ``code == 0`` reply is recovered separately by
    :func:`_native_warning`, so it is not silently dropped.
    """
    if isinstance(value, str) and value.strip().lower().startswith("error"):
        return value
    if isinstance(value, dict):
        result = value.get("result")
        if isinstance(result, str) and result.strip().lower().startswith("error"):
            return result
        parsed = _native_error_parts(value)
        if parsed is not None:
            text, code = parsed
            if not _is_native_success_code(code):
                return f"{text} (code {code})" if code is not None else text
    return None


def _native_warning(value: Any) -> str | None:
    """Recover the firmware's own text from an otherwise-successful native reply.

    A native reply can carry a truthy ``"error"`` string alongside an explicit
    ``code == 0`` — :func:`_native_error` treats that as success (see its
    docstring, live test 2026-09-24), but the odd text is still worth surfacing
    rather than discarding. Callers merge this into their success dict as an
    additive ``warning`` field. Returns ``None`` when there is nothing to warn
    about — no ``"error"`` text, or a genuine failure (handled instead by
    :func:`_native_error`/:func:`_native_fail`).
    """
    if isinstance(value, dict):
        parsed = _native_error_parts(value)
        if parsed is not None:
            text, code = parsed
            if _is_native_success_code(code):
                return text
    return None


#: get_solve_result's native code for "the solve has not finished yet"
#: (HARDWARE-OBSERVED, fw 8.46, live test 2026-09-24: ``{"error": "no solve
#: data", "code": 215}`` right after start_solve). Meaningful ONLY inside
#: :meth:`SeestarController.plate_solve`'s polling loop — everywhere else a
#: native 215 is an ordinary error, like any other nonzero code.
_SOLVE_IN_PROGRESS_CODE = 215

#: Floor for plate_solve's polling interval (fix round 1, review finding F1,
#: 2026-09-24): waited_s advances by this amount per iteration regardless of
#: the caller's poll_interval_s, so poll_interval_s<=0 against a device stuck
#: at code 215 cannot loop forever -- confirmed live to hang past a 3s
#: wall-clock guard before this floor existed.
_MIN_POLL_INTERVAL_S = 0.1


def _native_solve_code(value: Any) -> Any:
    """Return a native error dict's ``code``, else ``None``.

    Used only by :meth:`SeestarController.plate_solve` to recognise
    ``_SOLVE_IN_PROGRESS_CODE`` while polling; a normal success reply (no
    truthy ``"error"``) yields ``None``, which never equals 215.
    """
    if isinstance(value, dict):
        parsed = _native_error_parts(value)
        if parsed is not None:
            return parsed[1]
    return None


def _extract_solve_fields(value: Any) -> dict:
    """Best-effort ra_deg/dec_deg/angle_deg/fov_deg/star_number/solve_duration_ms.

    Pulled from ``get_solve_result``'s nested ``result`` object (RA converted
    from hours to degrees). Never raises: any missing or malformed key yields
    ``None`` for that field rather than a crash, mirroring
    :func:`_extract_focus_pos`.

    These are the *solver's reported position*, not the field centre.
    HARDWARE-OBSERVED (fw 8.46, live test 2026-09-24): this reported position
    sits near the commanded target even when the object is well off-centre in
    the frame — M1's nebula sat ~23' off-centre in the averaged raw subs
    (matching the live-stack Annotate position) while the solved ``ra_dec``
    sat only ~4' from the M1 catalog position, and the FITS header RA/DEC
    ~1'. For framing, use the stack Annotate pixel position
    (``get_view_state``), not these fields.
    """
    fields: dict[str, Any] = {
        "ra_deg": None,
        "dec_deg": None,
        "angle_deg": None,
        "fov_deg": None,
        "star_number": None,
        "solve_duration_ms": None,
    }
    nested = value.get("result") if isinstance(value, dict) else None
    if not isinstance(nested, dict):
        return fields
    ra_dec = nested.get("ra_dec")
    if (
        isinstance(ra_dec, (list, tuple))
        and len(ra_dec) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in ra_dec)
    ):
        fields["ra_deg"] = ra_dec[0] * 15
        fields["dec_deg"] = ra_dec[1]
    fields["angle_deg"] = nested.get("angle")
    fov = nested.get("fov")
    if isinstance(fov, (list, tuple)) and len(fov) == 2:
        fields["fov_deg"] = list(fov)
    fields["star_number"] = nested.get("star_number")
    fields["solve_duration_ms"] = nested.get("duration_ms")
    return fields


def _native_fail(value: Any, **extra: Any) -> dict | None:
    """Return an ``ok:false`` envelope if ``value`` is a native error, else None.

    Wraps :func:`_native_error` so every controller method that hands a native
    ``method_sync`` result back to the caller can guard it uniformly::

        if (bad := _native_fail(result)) is not None:
            return bad

    ``extra`` carries through any context fields (e.g. ``session_id`` on a goto)
    so a surfaced error still reports what was attempted.
    """
    err = _native_error(value)
    if err is not None:
        return {"ok": False, "error": err, "raw": value, **extra}
    return None


def _extract_focus_pos(focus: Any) -> int | None:
    """Best-effort focuser-position int from a native focuser response.

    HARDWARE-VALIDATED (Seestar S50, firmware 8.46): the reply is the standard
    JSON-RPC envelope with the value nested one level down —
    ``{"method": "get_focuser_position", "result": {"step": 1534}, ...}``. This
    used to read only the top level, so a live scope reporting step 1534 returned
    ``None``: ``get_focuser_position`` answered ``focus_pos: null`` and
    ``run_autofocus`` silently failed to seed the Tier-1 focus-drift baseline.

    The envelope is unwrapped first (as :func:`_parse_gps` already did), then the
    flat shapes are still accepted so a firmware that returns a bare number or a
    top-level key keeps working.
    """
    if isinstance(focus, bool):
        return None
    if isinstance(focus, (int, float)):
        return int(focus)
    if not isinstance(focus, dict):
        return None

    def _from(d: Any) -> int | None:
        if isinstance(d, bool):
            return None
        if isinstance(d, (int, float)):
            return int(d)
        if not isinstance(d, dict):
            return None
        for key in ("step", "focus_pos", "focuser_position", "position", "value"):
            val = d.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return int(val)
        return None

    # Nested envelope wins; fall back to the flat shape.
    nested = _from(focus.get("result"))
    return nested if nested is not None else _from(focus)


def _parse_gps(dev: Any) -> tuple[float, float] | None:
    """Extract the scope's ``(lat, lon)`` from a ``get_device_state`` response.

    HARDWARE-VALIDATED (Seestar S50, firmware 7.75): the GPS lives at
    ``result.location_lon_lat`` as a ``[lon, lat]`` pair (longitude FIRST). That
    validated shape is tried first. For resilience across firmware, the older
    guessed shapes are kept as fallbacks: ``setting.lat``/``lon``,
    ``location.lat``/``lon``, or top-level ``lat``/``lon``. Any malformed/empty
    input or a missing/non-numeric pair returns ``None`` (GPS unknown → the
    caller fails safe by assuming the saved site).
    """
    if not isinstance(dev, dict) or not dev:
        return None
    root = dev.get("result") if isinstance(dev.get("result"), dict) else dev
    if not isinstance(root, dict):
        return None

    def _num(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    # Validated shape (fw 7.75): [lon, lat].
    pair = root.get("location_lon_lat")
    if isinstance(pair, (list, tuple)) and len(pair) == 2 and _num(pair[0]) and _num(pair[1]):
        return (float(pair[1]), float(pair[0]))

    for src in (root.get("setting"), root.get("location"), root):
        if not isinstance(src, dict):
            continue
        lat, lon = src.get("lat"), src.get("lon")
        if _num(lat) and _num(lon):
            return (float(lat), float(lon))
    return None


def _parse_device_health(dev: Any) -> tuple[bool, bool]:
    """Extract ``(connected, verified)`` from a ``get_device_state`` reply.

    HARDWARE-VERIFIED (2026-07-12): ``is_verified`` is nested at
    ``result.device.is_verified`` — NOT at the top level. Reading it flat made
    the guardrail always report "unverified" and false-trip ``park_and_stop``.
    Battery IS in this same reply, at ``result.pi_status.battery_capacity`` (see
    :func:`_parse_battery`) — an earlier note here claimed otherwise and cost a
    redundant ``pi_get_info`` round-trip per check. Falls back to a flat dict for
    simple mocks.
    Any malformed/empty input fails SAFE to ``(False, False)``.
    """
    if not isinstance(dev, dict) or not dev:
        return (False, False)
    result = dev.get("result")
    device = result.get("device") if isinstance(result, dict) else None
    src = device if isinstance(device, dict) and device else dev
    verified = bool(src.get("is_verified", src.get("verified", False)))
    return (True, verified)


def _parse_mount_state(dev: Any) -> tuple[bool | None, bool | None]:
    """Extract ``(parked, tracking)`` from a ``get_device_state`` reply.

    HARDWARE-VALIDATED (fw 7.75 and 8.46): ``result.mount.close`` is ``True``
    when the arm is folded — the authoritative park signal — and
    ``result.mount.tracking`` is the device's own tracking flag. Alpaca's
    ``/atpark`` and ``/tracking`` disagree with both on this hardware (see
    CLAUDE.md), which is why ``get_status`` carries these beside Alpaca's
    ``tracking`` (2026-09-22 review remediation, task 5).

    Falls back to a flat ``mount`` dict for simple mocks. Each value is a real
    ``bool`` or ``None``: an unexpected shape, a missing field or a non-bool
    value is UNKNOWN, never guessed — a misread park signal is worse than none.
    """
    if not isinstance(dev, dict):
        return (None, None)
    result = dev.get("result")
    mount = result.get("mount") if isinstance(result, dict) else None
    if not isinstance(mount, dict):
        mount = dev.get("mount")
    if not isinstance(mount, dict):
        return (None, None)
    close, tracking = mount.get("close"), mount.get("tracking")
    return (
        close if isinstance(close, bool) else None,
        tracking if isinstance(tracking, bool) else None,
    )


def _parse_battery(info: Any) -> float | None:
    """Extract battery percent from a ``get_device_state`` or ``pi_get_info`` reply.

    HARDWARE-VERIFIED (fw 7.75): battery is at ``result.pi_status.battery_capacity``
    in ``get_device_state`` — the reply the guardrail already fetches for
    connected/verified — and at ``result.battery_capacity`` in ``pi_get_info``.

    A 2026-07-12 diagnosis found it was not at the *top level* of
    ``get_device_state`` and concluded it was absent entirely, which cost a second
    native round-trip per guardrail check (610 of them in one measured dashboard
    session). Both shapes are accepted here so either reply works; unknown or
    malformed input → ``None``, which the guardrail core treats as "battery
    unknown" rather than a stop.
    """
    if not isinstance(info, dict):
        return None
    result = info.get("result")
    root = result if isinstance(result, dict) else info
    # get_device_state nests it one level down under pi_status.
    candidates = [root]
    pi_status = root.get("pi_status")
    if isinstance(pi_status, dict):
        candidates.insert(0, pi_status)
    for src in candidates:
        for key in ("battery_capacity", "battery", "bat_capacity"):
            val = src.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val)
    return None


def _normalize_annotation_name(text: Any) -> str | None:
    """Case/whitespace-fold an annotation or target name for matching.

    "normalise case and spaces, so 'M 57' matches 'M57'" (task 5 brief,
    2026-09-24). Annotation names can be non-ASCII (e.g. "ν1 Lyr");
    ``str.lower()`` handles that fine. Returns ``None`` for anything that is
    not a non-empty string, so callers can skip matching without raising.
    """
    if not isinstance(text, str):
        return None
    folded = "".join(text.split()).lower()
    return folded or None


def _summarize_view_state(state: Any) -> tuple[bool, dict | None]:
    """Derive ``(observing, stack)`` from a raw ``get_view_state`` reply.

    Used by :meth:`SeestarController.get_view_state` and (task 6) the slot
    watcher, so it is a pure function next to the other parsers rather than
    inlined -- both need the identical read of ``observing``.

    ``observing`` is ``True`` ONLY when ``View.state == "working"`` and
    ``View.mode != "none"``. HARDWARE-OBSERVED (live test 2026-09-24): a
    freshly booted scope answers ``result: {}`` (no ``View`` at all), but a
    PARKED scope does NOT -- it keeps the ended session's ``View``, with
    ``state: "cancel"`` and ``mode: "none"``. So "observing" cannot be
    "result is non-empty"; it must read ``View.state``/``View.mode``
    specifically. Observed ``View.state`` values are ``"working"`` (active)
    and ``"cancel"`` (ended/stopped); ``"complete"`` appears on sub-steps, and
    a ``"fail"`` value is assumed to exist but was not observed live.

    ``stack`` is ``None`` only when there is no ``View`` at all (``result:
    {}``, missing, or a malformed payload). It is present whenever a ``View``
    exists -- INCLUDING an ended/parked session, so its final
    stacked/dropped/frame_errcode counts stay visible -- with these keys,
    every one ``None`` when absent from the payload rather than omitted:
    ``target_name``, ``view_state`` (``View.state``), ``mode``, ``stage``,
    ``state`` (``Stack.state``), ``lp_filter``, ``stacked``
    (``Stack.stacked_frame``), ``dropped`` (``Stack.dropped_frame``),
    ``frame_errcode``, ``solve_ra_deg``/``solve_dec_deg`` (from
    ``Stack.PlateSolve.ra_dec``, RA hours x 15 -- the solver's REPORTED
    position, not the field centre, per ``_extract_solve_fields``),
    ``annotate_state``, and ``target_px``/``target_radius_px`` -- the
    ``Stack.Annotate`` annotation whose ``names`` match ``target_name`` after
    :func:`_normalize_annotation_name`, or ``None`` when there is no match.
    A missing ``Stack``, a non-dict ``Stack``/``Annotate``, or any other odd
    shape degrades individual fields to ``None`` rather than raising.
    """
    result = state.get("result") if isinstance(state, dict) else None
    view = result.get("View") if isinstance(result, dict) else None
    if not isinstance(view, dict):
        return (False, None)

    view_state = view.get("state")
    mode = view.get("mode")
    observing = view_state == "working" and mode != "none"

    stack_raw = view.get("Stack")
    stack_src = stack_raw if isinstance(stack_raw, dict) else {}

    def _num(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    solve_ra_deg: float | None = None
    solve_dec_deg: float | None = None
    plate_solve = stack_src.get("PlateSolve")
    if isinstance(plate_solve, dict):
        ra_dec = plate_solve.get("ra_dec")
        if isinstance(ra_dec, (list, tuple)) and len(ra_dec) == 2 and _num(ra_dec[0]) and _num(ra_dec[1]):
            solve_ra_deg = ra_dec[0] * 15
            solve_dec_deg = ra_dec[1]

    annotate = stack_src.get("Annotate")
    annotate_state: Any = None
    target_px: list[float] | None = None
    target_radius_px: float | None = None
    if isinstance(annotate, dict):
        annotate_state = annotate.get("state")
        target_key = _normalize_annotation_name(view.get("target_name"))
        annotate_result = annotate.get("result")
        annotations = (
            annotate_result.get("annotations")
            if isinstance(annotate_result, dict)
            else None
        )
        if target_key is not None and isinstance(annotations, list):
            for ann in annotations:
                if not isinstance(ann, dict):
                    continue
                names = ann.get("names")
                if not isinstance(names, list):
                    continue
                if not any(_normalize_annotation_name(n) == target_key for n in names):
                    continue
                px, py = ann.get("pixelx"), ann.get("pixely")
                if _num(px) and _num(py):
                    target_px = [px, py]
                radius = ann.get("radius")
                if _num(radius):
                    target_radius_px = radius
                break

    stack = {
        "target_name": view.get("target_name"),
        "view_state": view_state,
        "mode": mode,
        "stage": view.get("stage"),
        "state": stack_src.get("state"),
        "lp_filter": view.get("lp_filter"),
        "stacked": stack_src.get("stacked_frame"),
        "dropped": stack_src.get("dropped_frame"),
        "frame_errcode": stack_src.get("frame_errcode"),
        "solve_ra_deg": solve_ra_deg,
        "solve_dec_deg": solve_dec_deg,
        "annotate_state": annotate_state,
        "target_px": target_px,
        "target_radius_px": target_radius_px,
    }
    return (observing, stack)


# ===========================================================================
# Thin MCP registration. The transport is stdio (mcp.run() default): this
# server opens NO inbound network port — Claude Code spawns it and speaks stdio,
# and must register it BEFORE a Remote Control session starts. Localhost/LAN
# bind concerns for seestar_alp (:5555) and the device data ports are handled in
# config.py / data_client.py, not here.
# ===========================================================================

mcp = FastMCP("seestar-mcp")

# Task 3 (2026-09-22 review remediation): FastMCP.__init__() just called the mcp
# package's configure_logging("INFO"), which is logging.basicConfig(level=INFO,
# handlers=[RichHandler(stderr)]) on the ROOT logger. httpx logs every request at
# INFO as `HTTP Request: GET <full-url> "HTTP/1.1 200 OK"`, and the meteoblue
# weather source (planning/weather.py) carries SEESTAR_METEOBLUE_API_KEY as the
# `apikey` query param, so every keyed weather fetch was writing the key to
# stderr -- and so to Claude Code's MCP logs, or journald on the Jetson. This is
# distinct from the provenance log, which already redacts the key (see
# provenance.py / SECURITY.md). Raising the httpx/httpcore loggers above INFO
# silences the request-line log at the source.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class _RedactApiKeyFilter(logging.Filter):
    """Defense in depth for the leak above: strip `apikey=<value>` from any
    record the httpx logger emits, independent of its level.

    A future change that lowers the httpx logger back to INFO/DEBUG (or a
    library path that logs the URL at WARNING+) must not silently re-open the
    leak; this filter keeps the key out regardless. See
    tests/test_logging_redaction.py, which pins both layers.
    """

    _PATTERN = re.compile(r"(apikey=)[^&\s\"]+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._PATTERN.sub(r"\1***REDACTED***", record.msg)
        if record.args:
            record.args = tuple(
                self._PATTERN.sub(r"\1***REDACTED***", str(arg))
                if self._PATTERN.search(str(arg))
                else arg
                for arg in record.args
            )
        return True


logging.getLogger("httpx").addFilter(_RedactApiKeyFilter())

_controller: SeestarController | None = None


def get_controller() -> SeestarController:
    """Return the lazily-built, cached controller singleton."""
    global _controller
    if _controller is None:
        _controller = SeestarController.from_settings()
    return _controller


def set_controller(controller: SeestarController | None) -> None:
    """Inject/replace the controller singleton (tests, lifespan management)."""
    global _controller
    _controller = controller


@mcp.tool()
async def connect_telescope() -> dict:
    """Connect to the Seestar via seestar_alp. No motion; safe to call anytime."""
    return await get_controller().connect_telescope()


@mcp.tool()
async def get_status() -> dict:
    """Read connection, RA/Dec pointing, and tracking/slewing state. Read-only.

    ``tracking`` is Alpaca's view and is known to disagree with the device on
    this hardware. ``mount_parked`` (arm folded) and ``mount_tracking`` are the
    authoritative native fields, read by one native ``get_device_state`` call;
    each is ``true``/``false``, or ``null`` when that read fails. Confirm a park
    with ``mount_parked``, not ``tracking``.
    """
    return await get_controller().get_status()


@mcp.tool()
async def get_view_state() -> dict:
    """Read the device's live view/stacking telemetry. Read-only.

    ``observing`` is ``true`` ONLY while an active session is running
    (``View.state == "working"`` and ``View.mode != "none"``); it is ``false``
    for a freshly-booted scope (``result: {}``), an ended/cancelled/parked
    session, or any other state. ``stack`` is a compact summary (target,
    stage, stacked/dropped counts, frame_errcode, plate-solve position, and
    the Annotate framing pixel position for the current target); it is
    ``null`` only when there is no View at all, and stays present -- with its
    final counts -- for an ended session. ``solve_ra_deg``/``solve_dec_deg``
    are the solver's REPORTED position, not the field centre; for framing use
    ``stack.target_px``.
    """
    return await get_controller().get_view_state()


@mcp.tool()
async def goto_target(
    name: str, ra: float, dec: float, use_lp_filter: bool = False
) -> dict:
    """Slew the telescope to a target and start a session.

    SIDE EFFECT: commands telescope MOTION and opens a new session manifest.
    ``ra``/``dec`` are the target coordinates; ``use_lp_filter`` toggles the
    light-pollution filter.
    """
    return await get_controller().goto_target(name, ra, dec, use_lp_filter)


@mcp.tool()
async def start_stack() -> dict:
    """Start live-stacking. SIDE EFFECT: begins capturing/integrating exposures."""
    return await get_controller().start_stack()


@mcp.tool()
async def stop_view(mode: str = "Stack") -> dict:
    """Stop the current view/stack. SIDE EFFECT: halts capture for the given mode.

    ``mode`` is ``"Stack"`` or ``"ContinuousExposure"``.
    """
    return await get_controller().stop_view(mode)


@mcp.tool()
async def run_autofocus() -> dict:
    """Run the autofocus routine. SIDE EFFECT: moves the focuser to refocus."""
    return await get_controller().run_autofocus()


@mcp.tool()
async def get_focuser_position() -> dict:
    """Read the current focuser position. Read-only."""
    return await get_controller().get_focuser_position()


@mcp.tool()
async def plate_solve() -> dict:
    """Plate-solve the current field and return the solution. Read-only pointing.

    Polls the device while the solve is in progress, up to ~30s, so this call
    can take that long to return. ``ra_deg``/``dec_deg`` report the solver's
    RA (converted from hours to degrees) and Dec — the solver's reported
    position, not necessarily the true field centre (fw 8.46 caveat: see
    SeestarController.plate_solve).
    """
    return await get_controller().plate_solve()


@mcp.tool()
async def get_run_state() -> dict:
    """Is an imaging run in progress right now? Read-only; makes no device call.

    Returns ``state``: ``"active"`` (a run is live), ``"idle"`` (nothing running),
    or ``"unknown"`` (a run was recorded but its stamp is too old to trust — the
    writer probably died mid-run). ``unknown`` is deliberately distinct from
    ``idle`` and must not be read as "the scope is free".

    ``run`` carries the record in BOTH ``active`` and stale-``unknown`` states (the
    stale record is kept on purpose — what was running when we lost track is worth
    knowing), and is ``None`` only for ``idle`` or an unreadable file. **Branch on
    ``state``, not on whether ``run`` is present** — a non-null ``run`` is not
    proof of a live session.

    Answers the question that is otherwise only inferable from a ``get_view_state``
    timeout, which produces a confident wrong answer in exactly the wrong
    direction.
    """
    return await get_controller().get_run_state()


@mcp.tool()
async def set_filter(position: int) -> dict:
    """Set the filter wheel position: 0 = dark, 1 = IRCUT, 2 = LP.

    SIDE EFFECT: physically moves the filter wheel to the given index.
    Index mapping hardware-verified on firmware 8.46; the device reports it via
    the native ``get_wheel_setting``.
    """
    return await get_controller().set_filter(position)


@mcp.tool()
async def set_dew_heater(on: bool) -> dict:
    """Turn the dew heater on or off.

    SIDE EFFECT: changes sensor temperature; enabling it INVALIDATES existing
    dark frames (rebuild darks afterwards).
    """
    return await get_controller().set_dew_heater(on)


@mcp.tool()
async def park() -> dict:
    """Park the telescope. SIDE EFFECT: stops tracking and moves the mount to park."""
    return await get_controller().park()


@mcp.tool()
async def shutdown() -> dict:
    """Power down the Seestar.

    SIDE EFFECT: this TERMINATES the seestar_alp control link — no further tool
    calls reach the device until it is powered back on.
    """
    return await get_controller().shutdown()


@mcp.tool()
async def list_subs(target: str | None = None) -> dict:
    """List RAW FITS subs saved on the device (optionally one target). Read-only."""
    return await get_controller().list_subs(target)


@mcp.tool()
async def download_subs(
    target: str | None = None,
    names: list[str] | None = None,
    dest: str | None = None,
) -> dict:
    """Download RAW subs to local storage (HTTP, SMB fallback).

    SIDE EFFECT: writes FITS files to the local data directory (hashed into the
    provenance log). Optionally filter by ``target`` and/or explicit ``names``.
    """
    return await get_controller().download_subs(target, names, dest)


@mcp.tool()
async def qa_tier1() -> dict:
    """Poll firmware telemetry once; return a snapshot + neutral health flags.

    Read-only. Flags are HEALTH signals for the anomaly playbook, not quality
    verdicts.
    """
    return await get_controller().qa_tier1()


@mcp.tool()
async def qa_tier2(target: str | None = None, paths: list[str] | None = None) -> dict:
    """Score RAW subs into PASS/MARGINAL/REJECT with per-sub reasons + keep-list.

    Read-only FITS analysis (photutils). Provide explicit ``paths`` or a
    ``target`` to glob the local data directory.

    NOTE: ``summary.target`` echoes the ``target`` argument and is therefore
    ``null`` whenever you call with ``paths=`` — it is not derived from the files.
    Do not build a header on it; use your own identifier for the session.
    """
    return await get_controller().qa_tier2(target, paths)


@mcp.tool()
async def qa_session_report(
    target: str | None = None, paths: list[str] | None = None
) -> dict:
    """Wind down a session: score subs, then WRITE a JSON+MD report and manifest.

    SIDE EFFECT: writes report and manifest files to local storage. Returns the
    keep-list and the artifact paths.
    """
    return await get_controller().qa_session_report(target, paths)


@mcp.tool()
async def get_site_profile() -> dict:
    """Return the saved observing-site profile (position, Bortle, horizon mask).

    Read-only. Returns ``ok:false`` if no profile has been set yet.
    """
    return await get_controller().get_site_profile()


@mcp.tool()
async def set_site_profile(
    name: str,
    lat: float,
    lon: float,
    elevation_m: float = 0.0,
    bortle: int | None = None,
    sqm: float | None = None,
    horizon_mask: list[list[float]] | None = None,
    min_altitude_deg: float = 20.0,
    field_rotation_ceiling_deg: float = 60.0,
) -> dict:
    """Save the observing-site profile every planning tool reads.

    SIDE EFFECT: writes a JSON profile to local storage. Captures position,
    sky darkness (Bortle/SQM), a horizon mask ``[[az_min, az_max, alt_min], ...]``
    and the usable-altitude sweet band. No device motion.
    """
    return await get_controller().set_site_profile(
        name,
        lat,
        lon,
        elevation_m,
        bortle,
        sqm,
        horizon_mask,
        min_altitude_deg,
        field_rotation_ceiling_deg,
    )


@mcp.tool()
async def assess_conditions(date: str | None = None) -> dict:
    """Go/no-go sky verdict for tonight from weather + moon over the dark window.

    Read-only. Only external call is one HTTPS GET to Open-Meteo; a weather
    outage is non-fatal (``go=null`` — assess the sky manually). ``date`` (ISO
    UTC instant) overrides "tonight": a bare site-local date (``YYYY-MM-DD``)
    means the night beginning on that date's evening; omitted means tonight (or
    the current night if already dark). Every verdict is reason-tagged.
    """
    return await get_controller().assess_conditions(date)


@mcp.tool()
async def get_target_observability(target: str, date: str | None = None) -> dict:
    """Observability of one named DSO tonight: altitude, sweet-band time, moon.

    Read-only, offline (deterministic astropy ephemeris). ``target`` is a
    catalog id or common name (e.g. ``"M27"`` / ``"Dumbbell Nebula"``); ``date``
    (ISO UTC instant) overrides "tonight": a bare site-local date
    (``YYYY-MM-DD``) means the night beginning on that date's evening; omitted
    means tonight (or the current night if already dark).

    The result's ``now`` block (``utc``/``alt_deg``/``az_deg``/``above_floor``/
    ``in_sweet_band``) is the target's position at the REAL current instant —
    unrelated to ``date``, which only selects which night the rest of the
    result (``observability``, ``dark_window_utc``) describes. Use ``now`` for
    a live heartbeat check against the floor/ceiling; use ``observability`` for
    the whole night's plan.
    """
    return await get_controller().get_target_observability(target, date)


@mcp.tool()
async def plan_targets(
    date: str | None = None,
    types: list[str] | None = None,
    min_alt: float | None = None,
    limit: int = 10,
    avoid_recent_days: int = 2,
    prefer_projects: bool = True,
) -> dict:
    """Rank tonight's best DSO targets for the Seestar given site, sky conditions,
    moon, light pollution, and alt-az field rotation. Returns a scored, reasoned
    shortlist.

    Read-only. Optionally filter by ``types`` and ``min_alt`` and cap the count
    with ``limit``. ``date`` (ISO UTC instant) overrides "tonight": a bare
    site-local date (``YYYY-MM-DD``) means the night beginning on that date's
    evening; omitted means tonight (or the current night if already dark). When
    ``prefer_projects`` (default) the projects/history store boosts targets that
    still need data and suppresses ones imaged within ``avoid_recent_days``.
    """
    return await get_controller().plan_targets(
        date, types, min_alt, limit, avoid_recent_days, prefer_projects
    )


@mcp.tool()
async def list_projects(detail: str = "summary") -> dict:
    """List all imaging projects with their goals and accumulated integration.

    Read-only. Empty when no projects have been created yet. ``detail="summary"``
    (default) omits each project's ``sessions`` history, replacing it with
    ``sessions_count`` and ``last_session_utc``; pass ``detail="full"`` for the
    complete session records.
    """
    return await get_controller().list_projects(detail=detail)


@mcp.tool()
async def get_project(target: str) -> dict:
    """Return one project's goal, collected integration, and session history.

    Read-only. ``target`` is a catalog id (e.g. ``"M31"``). Returns ``ok:false``
    if no project exists for it yet.
    """
    return await get_controller().get_project(target)


@mcp.tool()
async def set_project_goal(target: str, goal_minutes: float) -> dict:
    """Set a target's total integration goal (minutes) for the observing planner.

    SIDE EFFECT: writes to the local projects store. Use ``goal_minutes=0`` for
    an open-ended project. Does not change already-collected integration.
    """
    return await get_controller().set_project_goal(target, goal_minutes)


@mcp.tool()
async def log_session_result(
    target: str,
    integration_minutes: float,
    subs_total: int,
    subs_kept: int,
    median_fwhm: float | None = None,
    notes: str = "",
) -> dict:
    """Record a finished imaging session for a target (integration + kept/total
    subs) into its project history; call at wind-down.

    SIDE EFFECT: appends a session and accumulates integration toward the goal in
    the local projects store (auto-completing the project when the goal is met).
    """
    return await get_controller().log_session_result(
        target, integration_minutes, subs_total, subs_kept, median_fwhm, notes
    )


@mcp.tool()
async def recommend_projects(
    limit: int | None = None, detail: str = "summary"
) -> dict:
    """Recommend active projects that still need data, most-needed first.

    Read-only. Answers "what should I image more of tonight?". ``limit`` caps the
    count. ``detail="summary"`` (default) omits each project's ``sessions``
    history; pass ``detail="full"`` for the complete records.
    """
    return await get_controller().recommend_projects(limit, detail=detail)


@mcp.tool()
async def simulate_night(
    date: str | None = None,
    types: list[str] | None = None,
    limit: int | None = None,
) -> dict:
    """Dry-run tonight's autonomous plan as an ordered target schedule WITHOUT
    moving the telescope. Use this to preview/confirm an autonomous night before
    it starts.

    Read-only/compute-only: ranks tonight's targets and packs them into the dark
    window, issuing zero device motion. ``date`` (ISO UTC instant) overrides
    "tonight": a bare site-local date (``YYYY-MM-DD``) means the night beginning
    on that date's evening; omitted means tonight (or the current night if
    already dark).
    """
    return await get_controller().simulate_night(date, types, limit)


@mcp.tool()
async def check_night_guardrails(
    session_start_utc: str,
    max_session_hours: float = 10.0,
    battery_floor_pct: float = 20.0,
    dawn_margin_min: float = 15.0,
) -> dict:
    """Evaluate hard-stop conditions for an unattended run (approaching dawn, low
    battery, weather no-go, lost connection, max session duration). Returns
    whether to continue or park-and-stop.

    Read-only: gathers live device health + weather and returns a verdict; fails
    SAFE to ``park_and_stop`` if the scope's health cannot be confirmed.
    """
    return await get_controller().check_night_guardrails(
        session_start_utc, max_session_hours, battery_floor_pct, dawn_margin_min
    )


@mcp.tool()
async def log_sky_result(
    target: str | None = None,
    az: float | None = None,
    alt: float | None = None,
    solved: bool = True,
    weather_go: bool | None = None,
) -> dict:
    """Log one plate-solve outcome so the learner can infer fixed obstructions.

    SIDE EFFECT: appends to the local weather-gated sky-failure histogram (no
    device motion). Pass explicit ``az``/``alt`` or a ``target`` (resolved against
    the saved site at now). ``solved=False`` records a failure; bad-weather
    failures (``weather_go=False``) are excluded from obstruction inference.
    """
    return await get_controller().log_sky_result(target, az, alt, solved, weather_go)


@mcp.tool()
async def suggest_horizon_mask() -> dict:
    """Suggest horizon-mask arcs learned from cross-night, weather-gated failures.

    READ-ONLY: returns candidate arcs with their evidence but NEVER edits the
    saved mask. Location-scoped to the saved site. Confirm a suggestion by calling
    ``add_horizon_mask`` explicitly.
    """
    return await get_controller().suggest_horizon_mask()


@mcp.tool()
async def add_horizon_mask(az_min: float, az_max: float, alt_min: float) -> dict:
    """Append one horizon-mask arc to the saved site profile (user confirm step).

    SIDE EFFECT: persists the ``(az_min, az_max, alt_min)`` arc to the site
    profile. This is the ONLY way the mask changes — suggestions never auto-apply.
    """
    return await get_controller().add_horizon_mask(az_min, az_max, alt_min)


def main() -> None:
    """Run the MCP server over stdio (no inbound network port is opened)."""
    mcp.run()


if __name__ == "__main__":
    main()
