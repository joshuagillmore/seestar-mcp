"""Tier-1 firmware-telemetry HEALTH monitor for the ZWO Seestar S50.

Tier-1 trusts the device firmware: it polls the Seestar's own ``get_view_state``
/ ``get_device_state`` telemetry (live-stack count, rejected-frame count,
plate-solve state/RMS, focuser position, per-frame HFD, tracking) and emits a
:class:`Tier1Snapshot` plus a small set of neutral, threshold-based *health
flags*.

IMPORTANT CONTRACT:
- These flags are HEALTH SIGNALS for the anomaly playbook to interpret -- they
  are NOT quality verdicts. Tier-1 never decides whether data is "good"; deciding
  keep/reject on FWHM / eccentricity / SNR is Tier-2's job. No FWHM is computed
  here.
- Alt-Az field rotation makes rejected-frame counts rise (often after ~15 min);
  late-session rejection is EXPECTED, not a fault. The flags merely describe the
  signal; interpretation lives elsewhere.

Device key names are NOT confirmed against firmware, so the parsers are tolerant
of several spellings. Every such assumption is marked ``# FIRMWARE-DEPENDENT``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from .config import Settings
    from .provenance import ProvenanceLog


@dataclass
class Tier1Snapshot:
    """One poll of firmware telemetry (all fields optional; unknown -> None)."""

    ts: str  # UTC ISO 8601
    stacked: int | None = None  # live-stacked frame count
    rejected: int | None = None  # rejected-frame count
    solve_ok: bool | None = None  # plate-solve state healthy?
    solve_rms: float | None = None
    focus_pos: int | None = None
    hfd: float | None = None  # half-flux-diameter focus-quality (if exposed)
    tracking: bool | None = None
    #: Reported target name (CONFIRMED on firmware 7.75: ``View.target_name``),
    #: when the firmware exposes one. Used only so Tier1Monitor can detect a
    #: target switch and reset its trend baseline (live test 2026-09-24: a
    #: switch from one target to another read as stacked_delta=-274 and a
    #: false ``stacking_stalled`` without this) -- never surfaced as a quality
    #: signal itself.
    target_name: str | None = None
    raw: dict = field(default_factory=dict)  # merged raw telemetry, for audit
    #: True when a telemetry read failed, so every metric above is "unknown"
    #: rather than "measured and fine". The detail lives in
    #: ``raw["view_error"]`` / ``raw["device_error"]``, but the tool boundary
    #: strips ``raw`` to keep output small — without this flag a total read
    #: failure is indistinguishable from a healthy idle scope (all-null
    #: snapshot, empty flags). Contract rule 4: unknown is not empty.
    degraded: bool = False


# --- tolerant coercion / lookup helpers -----------------------------------

# Tokens (lowercased) that a firmware status string may use for a "healthy" /
# "on" state.  # FIRMWARE-DEPENDENT
_POSITIVE_TOKENS = {
    "ok",
    "true",
    "yes",
    "on",
    "1",
    "complete",
    "completed",
    "success",
    "solved",
    "done",
    "tracking",
    "working",
}


def _first(d: dict, *keys: str) -> Any:
    """Return the first present, non-None value among ``keys`` in ``d``."""
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return None


def _unwrap(d: Any) -> dict:
    """Probe one level of common telemetry wrappers tolerantly.

    Seestar/Alpaca payloads sometimes nest the real state under ``View``,
    ``result`` or ``Value``.  # FIRMWARE-DEPENDENT
    """
    if not isinstance(d, dict):
        return {}
    for wrapper in ("View", "result", "Value"):
        inner = d.get(wrapper)
        if isinstance(inner, dict):
            return inner
    return d


def _first_in(dicts: tuple[Any, ...], *keys: str) -> Any:
    """Return the first present, non-None value across several dicts.

    ``dicts`` are searched in order; within each, ``_first`` picks the first
    present key.  Non-dict entries are skipped.
    """
    for d in dicts:
        if isinstance(d, dict):
            value = _first(d, *keys)
            if value is not None:
                return value
    return None


def _locate_view(d: Any) -> dict:
    """Descend the action-tunnel wrappers to the ``View`` telemetry dict.

    CONFIRMED against live Seestar S50 firmware 7.75 (2026-07-05): the native
    ``get_view_state`` result is wrapped ``Value`` -> ``result`` -> ``View``,
    e.g. ``{"result": {"View": {...}}}``.  We peel the ``Value``/``result``
    wrappers (in any order, bounded) and then return the ``View`` sub-object.

    Kept tolerant for other firmware / mocked shapes: if no ``View`` is found we
    return the deepest unwrapped dict, and a plain flat dict is returned as-is.
    """
    if not isinstance(d, dict):
        return {}
    cur = d
    for _ in range(4):  # bounded peel of Value/result wrappers
        nxt = None
        for wrapper in ("Value", "result"):
            inner = cur.get(wrapper)
            if isinstance(inner, dict):
                nxt = inner
                break
        if nxt is None:
            break
        cur = nxt
    view = cur.get("View")
    if isinstance(view, dict):
        return view
    return cur


def _coerce_bool(value: Any) -> bool | None:
    """Coerce a bool / number / status-string into a tri-state bool."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if not token:
            return None
        return token in _POSITIVE_TOKENS
    return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- parsers (partial fields; missing key -> field omitted) ---------------


def _parse_view_state(d: dict) -> dict:
    """Parse ``get_view_state`` telemetry: stacking, rejection, solve, HFD.

    CONFIRMED against live Seestar S50 firmware 7.75 (2026-07-05): the real
    payload is nested ``result.View`` with the live-stack counters under
    ``View.Stack`` (``stacked_frame``/``dropped_frame``) and the plate-solve /
    annotation signal under ``View.Stack.Annotate`` (``state == "complete"``).
    The overall stage/state live on ``View`` (``stage``: "Initialise",
    "Stack", ...; ``state``: "working"/"complete").

    We descend into ``View`` and then ``View.Stack`` (when present), but keep
    tolerant flat/alternate-key fallbacks so older firmware and mocked shapes
    still parse.  # FIRMWARE-DEPENDENT (fallback spellings for other firmware)
    """
    view = _locate_view(d)
    out: dict[str, Any] = {}

    # Overall stage/state (read from View); ``Stack`` carries the counters
    # while stacking.  During "Initialise" there is no Stack sub-object.
    stack = view.get("Stack")
    if not isinstance(stack, dict):
        stack = {}

    # Counters live under View.Stack on real firmware; fall back to the View /
    # flat level for older or mocked shapes.
    stacked = _as_int(
        _first_in((stack, view), "stacked_frame", "stacked", "stack_count", "lapse_count")
    )
    if stacked is not None:
        out["stacked"] = stacked

    rejected = _as_int(_first_in((stack, view), "dropped_frame", "rejected", "bad_count"))
    if rejected is not None:
        out["rejected"] = rejected

    # Target name: CONFIRMED on firmware 7.75 at the View level, not under
    # Stack (see REAL_VIEW_STATE in tests/test_qa_tier1.py). Tier1Monitor uses
    # a change here to detect a target switch and reset its trend baseline --
    # see ``Tier1Monitor._is_new_stack``.
    target_name = _first_in((view, stack), "target_name", "target")
    if target_name is not None:
        out["target_name"] = target_name

    # Plate-solve / annotation success: on firmware 7.75 this is signalled by
    # View.Stack.Annotate.state == "complete".  Only a "complete" annotation
    # implies solve_ok=True; anything else (working/absent, e.g. Initialise)
    # leaves solve_ok as None -- never forced False here.
    solve_ok: bool | None = None
    annotate = stack.get("Annotate")
    if isinstance(annotate, dict):
        ann_state = annotate.get("state")
        if isinstance(ann_state, str) and ann_state.strip().lower() == "complete":
            solve_ok = True
    if solve_ok is None:
        # Tolerant fallback for older/mocked flat shapes.
        solve_ok = _coerce_bool(
            _first_in((stack, view), "plate_solve", "solve", "solve_state")
        )
    if solve_ok is not None:
        out["solve_ok"] = solve_ok

    solve_rms = _as_float(_first_in((stack, view), "rms", "solve_rms"))
    if solve_rms is not None:
        out["solve_rms"] = solve_rms

    # HFD: average hfd_x/hfd_y when both present, else single value.
    hfd_x = _as_float(_first_in((stack, view), "hfd_x"))
    hfd_y = _as_float(_first_in((stack, view), "hfd_y"))
    if hfd_x is not None and hfd_y is not None:
        out["hfd"] = (hfd_x + hfd_y) / 2
    else:
        hfd = _as_float(_first_in((stack, view), "hfd", "hfd_x"))
        if hfd is not None:
            out["hfd"] = hfd

    return out


def _parse_device_state(d: dict) -> dict:
    """Parse ``get_device_state`` telemetry: focuser position, tracking.

    Key names are tolerant/unconfirmed.  # FIRMWARE-DEPENDENT
    """
    d = _unwrap(d)
    out: dict[str, Any] = {}

    focus_pos = _as_int(_first(d, "focus_pos", "focuser_position", "step"))
    if focus_pos is not None:
        out["focus_pos"] = focus_pos

    tracking = _coerce_bool(_first(d, "tracking", "track_state"))
    if tracking is not None:
        out["tracking"] = tracking

    return out


class Tier1Monitor:
    """Polls firmware telemetry and emits neutral health flags.

    The threshold parameters here are Tier-1 HEALTH thresholds -- distinct from
    the Tier-2 QA thresholds carried in :class:`~seestar_mcp.config.Settings`.
    """

    def __init__(
        self,
        alpaca: Any,
        *,
        provenance: ProvenanceLog | None = None,
        stall_polls: int = 2,
        rejection_spike_delta: int = 10,
        focus_drift_tol: int = 25,
        hfd_rise_tol: float = 0.5,
    ) -> None:
        self._alpaca = alpaca
        self._provenance = provenance
        self.stall_polls = stall_polls
        self.rejection_spike_delta = rejection_spike_delta
        self.focus_drift_tol = focus_drift_tol
        self.hfd_rise_tol = hfd_rise_tol
        self.history: list[Tier1Snapshot] = []
        self.focus_baseline: int | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        alpaca: Any,
        provenance: ProvenanceLog | None = None,
    ) -> Tier1Monitor:
        """Build a monitor with default Tier-1 thresholds.

        ``settings`` currently carries no Tier-1 fields; only ``alpaca`` and
        ``provenance`` are wired through. Do not read Tier-2 QA thresholds here.
        """
        return cls(alpaca, provenance=provenance)

    # --- polling ----------------------------------------------------------

    async def _safe_call(self, method: str) -> tuple[dict, str | None]:
        """Call ``method_sync(method)``; on ``AlpacaError`` return ({}, error).

        A single failed sub-call must not crash a poll, so the failure is
        captured (and later recorded in ``raw``) rather than raised.
        """
        try:
            result = await self._alpaca.method_sync(method)
        except Exception as exc:  # noqa: BLE001 - the whole point of _safe_call
            # Deliberately broader than AlpacaError. This used to catch only
            # AlpacaError, so anything else (a bare httpx/OS error, a timeout, a
            # decode failure) escaped poll(), escaped the controller's
            # `except AlpacaError`, and reached the MCP boundary as a raw
            # exception - breaking the never-raise-on-tool-paths convention that
            # this method's own name and docstring promise.
            return {}, f"{type(exc).__name__}: {exc}"
        return (result if isinstance(result, dict) else {}), None

    async def poll(self) -> Tier1Snapshot:
        """Poll view + device telemetry into one :class:`Tier1Snapshot`."""
        view, view_err = await self._safe_call("get_view_state")
        dev, dev_err = await self._safe_call("get_device_state")

        view_fields = _parse_view_state(view)
        dev_fields = _parse_device_state(dev)

        raw: dict[str, Any] = {"view": view, "device": dev}
        if view_err is not None:
            raw["view_error"] = view_err
        if dev_err is not None:
            raw["device_error"] = dev_err

        snapshot = Tier1Snapshot(
            ts=datetime.now(timezone.utc).isoformat(),
            stacked=view_fields.get("stacked"),
            rejected=view_fields.get("rejected"),
            solve_ok=view_fields.get("solve_ok"),
            solve_rms=view_fields.get("solve_rms"),
            focus_pos=dev_fields.get("focus_pos"),
            hfd=view_fields.get("hfd"),
            tracking=dev_fields.get("tracking"),
            target_name=view_fields.get("target_name"),
            degraded=(view_err is not None or dev_err is not None),
            raw=raw,
        )

        self.history.append(snapshot)
        if self.focus_baseline is None and snapshot.focus_pos is not None:
            self.focus_baseline = snapshot.focus_pos

        if self._provenance is not None:
            self._provenance.log_call(
                tool="qa_tier1.poll",
                args={
                    "stacked": snapshot.stacked,
                    "rejected": snapshot.rejected,
                    "solve_ok": snapshot.solve_ok,
                    "focus_pos": snapshot.focus_pos,
                },
            )

        return snapshot

    def set_focus_baseline(self, pos: int) -> None:
        """Set the reference focuser position used for drift trends."""
        self.focus_baseline = pos

    # --- derived signals --------------------------------------------------

    @staticmethod
    def _is_new_stack(prev: Tier1Snapshot, latest: Tier1Snapshot) -> bool:
        """True when ``latest`` starts a new stack relative to ``prev``.

        Live test 2026-09-24: consecutive polls across a ``goto_target`` gave
        stacked 301 then 24 on the new target. The trend logic compared them
        directly (stacked_delta=-274) and flagged a false ``stacking_stalled``
        because it never knew the target had changed. Firmware resets the
        live-stack counters for a new target, so a stack restarts when:

        - the reported target name changes (only when the firmware actually
          reports one for both polls -- an absent name proves nothing), or
        - ``stacked`` decreases -- a legitimate stack only counts up, so any
          decrease means a new one started even if no target name is exposed.
        """
        if (
            prev.target_name is not None
            and latest.target_name is not None
            and prev.target_name != latest.target_name
        ):
            return True
        if (
            prev.stacked is not None
            and latest.stacked is not None
            and latest.stacked < prev.stacked
        ):
            return True
        return False

    def _current_stack_start(self) -> int:
        """Index into ``history`` where the current (most recent) stack began.

        Recomputed from ``history`` on every call (trends/check already do
        this rather than caching), so it works whether ``history`` was built
        by :meth:`poll` or assigned directly, e.g. in tests.
        """
        start = 0
        for i in range(1, len(self.history)):
            if self._is_new_stack(self.history[i - 1], self.history[i]):
                start = i
        return start

    def trends(self) -> dict:
        """Compute inter-poll deltas. Safe with fewer than 2 snapshots.

        ``stacked_delta``/``rejected_delta``/``hfd_delta`` only ever compare
        within the current stack (see ``_current_stack_start``) -- a poll that
        starts a new stack reports ``None`` for these rather than a delta
        against the previous target's counters.
        """
        out: dict[str, Any] = {
            "stacked_delta": None,
            "rejected_delta": None,
            "focus_delta": None,
            "hfd_delta": None,
        }

        if self.history:
            latest = self.history[-1]
            if latest.focus_pos is not None and self.focus_baseline is not None:
                out["focus_delta"] = latest.focus_pos - self.focus_baseline

        stack = self.history[self._current_stack_start() :]
        if len(stack) >= 2:
            prev = stack[-2]
            latest = stack[-1]
            if prev.stacked is not None and latest.stacked is not None:
                out["stacked_delta"] = latest.stacked - prev.stacked
            if prev.rejected is not None and latest.rejected is not None:
                out["rejected_delta"] = latest.rejected - prev.rejected
            if prev.hfd is not None and latest.hfd is not None:
                out["hfd_delta"] = latest.hfd - prev.hfd

        return out

    def check(self) -> list[str]:
        """Return neutral HEALTH FLAGS (not verdicts) from history+thresholds.

        Each flag is a signal for the anomaly playbook to interpret. In Alt-Az,
        a late-session ``rejection_spike`` is expected, not necessarily a fault.
        """
        flags: list[str] = []
        trends = self.trends()

        # Stacking stalled: last ``stall_polls`` snapshots of the CURRENT stack
        # show no increase. Restricted to the current stack (see
        # ``_current_stack_start``) so a target switch is never mistaken for a
        # stall (live test 2026-09-24).
        stack = self.history[self._current_stack_start() :]
        if len(stack) >= self.stall_polls:
            window = stack[-self.stall_polls :]
            stacked_vals = [snap.stacked for snap in window]
            if all(v is not None for v in stacked_vals):
                if stacked_vals[-1] <= stacked_vals[0]:
                    flags.append("stacking_stalled")

        rejected_delta = trends["rejected_delta"]
        if rejected_delta is not None and rejected_delta >= self.rejection_spike_delta:
            flags.append("rejection_spike")

        focus_delta = trends["focus_delta"]
        if focus_delta is not None and abs(focus_delta) > self.focus_drift_tol:
            flags.append("focus_drift")

        if self.history and self.history[-1].solve_ok is False:
            flags.append("solve_lost")

        hfd_delta = trends["hfd_delta"]
        if hfd_delta is not None and hfd_delta > self.hfd_rise_tol:
            flags.append("hfd_rising")

        return flags

    def status_line(self) -> str:
        """Compact one-liner for the phone UI; omits unknown fields gracefully."""
        if not self.history:
            return ""
        latest = self.history[-1]
        trends = self.trends()
        parts: list[str] = []

        if latest.stacked is not None:
            stacked_delta = trends["stacked_delta"]
            if stacked_delta is not None:
                parts.append(f"stacked {latest.stacked} ({stacked_delta:+d})")
            else:
                parts.append(f"stacked {latest.stacked}")

        if latest.rejected is not None:
            parts.append(f"rejected {latest.rejected}")

        if latest.solve_ok is not None:
            parts.append("solve OK" if latest.solve_ok else "solve LOST")

        focus_delta = trends["focus_delta"]
        if focus_delta is not None:
            parts.append(f"focus Δ={focus_delta:+d}")

        if latest.hfd is not None:
            parts.append(f"hfd {latest.hfd:.2f}")

        return " | ".join(parts)
