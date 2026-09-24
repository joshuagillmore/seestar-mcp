"""Pure parsers for the device's native JSON-RPC replies.

seestar_alp tunnels each native reply verbatim through the Alpaca action
endpoint; these functions read those replies without touching the network.

Why a module of its own (task 6, live test 2026-09-24): the slot watcher
(:mod:`seestar_mcp.slot_watch`) must read ``get_view_state`` exactly as the
``get_view_state`` tool does -- the same ``observing`` rule and the same
native-error test -- or a failed read in one would be a session end in the
other. Both used to live in ``server.py``, but importing ``server.py`` builds
the FastMCP app (which reconfigures root logging) and pulls in astropy and
photutils: about 5 s on the dev PC against about 0.7 s without, for a watcher
that is re-armed every 29 minutes. ``server.py`` imports these names back, so
``seestar_mcp.server._native_error`` and friends still resolve.
"""

from __future__ import annotations

from typing import Any


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
    watcher, so it is a pure function rather than inlined -- both need the
    identical read of ``observing``.

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
    ``Stack.PlateSolve.ra_dec``, RA hours x 15 -- the solved field centre in
    JNow, the equinox of date; corrected by the goto-epoch finding of
    2026-09-24, which traced the earlier "reported position, not the field
    centre" caveat to J2000-vs-JNow precession, see
    ``server._extract_solve_fields``; ``server.get_view_state`` adds the J2000
    equivalent as ``solve_ra_j2000_deg``/``solve_dec_j2000_deg``, so this
    parser stays free of the clock and astropy),
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
