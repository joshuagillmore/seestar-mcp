"""Quiet slot watcher: one line per stacking event, from ``get_view_state`` alone.

Live test 2026-09-24: a scratch script polling ``get_view_state`` once a minute
under a Claude Code Monitor kept a whole night observable without noise, because
it spoke only when something happened. This is that script made a supported
tool: a pure :class:`SlotWatcher` that turns successive replies into event
lines, plus a CLI loop that polls and prints them.

USAGE
    uv --directory /path/to/SeeStar-AI run python -m seestar_mcp.slot_watch \\
        --duration 1740 --every 60 --drop-burst 3 --milestone 60 --stall-polls 3

    Under a Claude Code Monitor. A Monitor kills its command after at most
    30 min, so the default window is 29 min and the last line says to re-arm:

    Monitor(description="Seestar slot events", timeout_ms=1800000,
            command="PYTHONIOENCODING=utf-8 uv --directory /path/to/SeeStar-AI "
                    "run python -m seestar_mcp.slot_watch --duration 1740")

    Only stdout is the event stream. Output is UTF-8 because target names can
    be non-ASCII. The module forces UTF-8 on its own stdout, and
    PYTHONIOENCODING=utf-8 covers a wrapper around it. If you filter the
    stream, use ``grep -a --line-buffered``: without ``-a``, grep can take a
    non-ASCII line for binary data and never print it.

    Run it IN ADDITION to the run-session heartbeat, never instead. An event
    stream is silent while nothing happens, and silence does not wake an agent
    (run-session, "An event Monitor is NOT a heartbeat").

EVENTS (each one flushed line, prefixed HH:MM:SSZ)
    stage=S target=T stacked=N dropped=M     the first poll, a stage change,
                                             a new target, or a new session
    idle: ...                                the first poll finds no session
    DROPS +K in one poll (...)               at least --drop-burst new drops
    milestone stacked=N dropped=M            every --milestone stacked frames
    STALL: stacked flat at N for K polls     --stall-polls flat polls in Stack
    ENDED: session ended (...)               ``observing`` turned false; the
                                             final counts and frame_errcode
    ERR ...                                  a failed or malformed poll; the
                                             watch carries on
    watch window ended (re-arm to keep watching)

DEVICE POSTURE
    Reads only ``get_view_state`` (the device-touching tier) once per
    ``--every`` seconds, with a floor of 10 s; 60 s is the proven cadence. It
    never reads the image share and never commands motion. Each poll is
    provenance-logged under the client id ``slot-watch``, or
    ``<SEESTAR_CLIENT_ID>/slot-watch`` when that is set.

Sessions are read with :func:`~seestar_mcp.native_reply._summarize_view_state`,
the same rule the ``get_view_state`` tool uses: ``observing`` means
``View.state == "working"`` and a mode other than ``"none"``. A parked scope
keeps the ended session's View (``state`` "cancel"), so "ended" means
``observing`` turned false, not ``result: {}``.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from .alpaca_client import AlpacaClient
from .config import get_settings
from .native_reply import _native_error, _summarize_view_state
from .provenance import ProvenanceLog

#: Floor for ``--every``. get_view_state is device-touching and competes with
#: the scope's control link while it stacks (run-session "three tiers of
#: traffic"); a typo such as ``--every 0`` must not become a busy loop on it.
_MIN_EVERY_S = 10.0

#: Provenance client id for this process. ProvenanceLog: several clients append
#: to one log, and without an id their traffic is indistinguishable -- an audit
#: must be able to tell the watcher's once-a-minute reads from the agent's.
_CLIENT_ID = "slot-watch"

_END_LINE = "watch window ended (re-arm to keep watching)"

#: Longest error text kept on an ERR line, so a raw reply cannot flood a Monitor.
_ERR_TEXT_LIMIT = 200


def _stamp(now_utc: datetime) -> str:
    """``HH:MM:SSZ``; a naive datetime is taken to be UTC already."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(timezone.utc).strftime("%H:%M:%SZ")


def _one_line(stamp: str, text: str) -> str:
    """One event is one line: a Monitor turns every stdout line into an event."""
    return f"{stamp} " + " ".join(text.splitlines())


def _clip(text: str) -> str:
    if len(text) <= _ERR_TEXT_LIMIT:
        return text
    return text[: _ERR_TEXT_LIMIT - 3] + "..."


def _fmt(value: Any) -> str:
    return "-" if value is None else str(value)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _describe(exc: BaseException) -> str:
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


class SlotWatcher:
    """Turns successive ``get_view_state`` replies into quiet event lines.

    Pure and stateful: :meth:`observe` takes one reply and the poll time, and
    returns the lines that poll earns -- usually none. Nothing here reads the
    clock or the network, so a scripted sequence of replies pins the behaviour.
    """

    def __init__(
        self, *, drop_burst: int = 3, milestone: int = 60, stall_polls: int = 3
    ) -> None:
        self.drop_burst = drop_burst
        self.milestone = milestone
        self.stall_polls = stall_polls
        #: ``None`` until the first readable reply, so the first poll can say
        #: what the watcher found instead of announcing a change.
        self._observing: bool | None = None
        self._stage: Any = None
        self._target: Any = None
        #: Last stack summary seen while observing, for an ENDED line when the
        #: View vanishes outright (``result: {}``) and takes its counts with it.
        self._last_stack: dict | None = None
        self._reset_counters()

    def _reset_counters(self) -> None:
        """Forget the per-session baselines (new target, new or ended session).

        A new session's counts are its own; comparing them with the last
        session's would invent drop bursts, milestones and stalls.
        """
        self._stacked: int | None = None
        self._dropped: int | None = None
        self._milestone_idx: int | None = None
        self._flat = 0

    def observe(self, view_state_reply: Any, now_utc: datetime) -> list[str]:
        """Return the event lines for one ``get_view_state`` reply; never raises."""
        stamp = _stamp(now_utc)
        # A failed read is an ERR, and it leaves every baseline alone. Were it
        # summarised, it would read as "no View" -- i.e. a session ending.
        err = _native_error(view_state_reply)
        if err is not None:
            return [_one_line(stamp, f"ERR get_view_state: {_clip(err)}")]
        result = (
            view_state_reply.get("result") if isinstance(view_state_reply, dict) else None
        )
        if not isinstance(result, dict):
            return [
                _one_line(
                    stamp,
                    f"ERR unexpected get_view_state reply: {_clip(repr(view_state_reply))}",
                )
            ]

        observing, stack = _summarize_view_state(view_state_reply)
        if observing:
            texts = self._observe_session(stack)
        else:
            texts = self._observe_idle(stack)
        return [_one_line(stamp, text) for text in texts]

    def observe_error(self, exc: BaseException, now_utc: datetime) -> list[str]:
        """The line for a poll that raised (bridge down, timeout, ...)."""
        return [_one_line(_stamp(now_utc), f"ERR poll failed: {_clip(_describe(exc))}")]

    def _observe_idle(self, stack: dict | None) -> list[str]:
        texts: list[str] = []
        if self._observing:
            # AMENDED (task 6, 2026-09-24): "idle" is `observing` turning false.
            # A parked or ended session keeps its View (state "cancel") with
            # the final counts; only a vanished View falls back to the last seen.
            if stack is not None:
                final, how = stack, f"view_state={_fmt(stack['view_state'])}"
            else:
                final, how = self._last_stack or {}, "view gone; last seen"
            texts.append(
                f"ENDED: session ended ({how}) target={_fmt(final.get('target_name'))} "
                f"stacked={_fmt(final.get('stacked'))} "
                f"dropped={_fmt(final.get('dropped'))} "
                f"errcode={_fmt(final.get('frame_errcode'))}"
            )
        elif self._observing is None:
            if stack is None:
                texts.append("idle: no view since boot (result is empty)")
            else:
                texts.append(
                    f"idle: no session running (view_state={_fmt(stack['view_state'])} "
                    f"target={_fmt(stack['target_name'])} "
                    f"stacked={_fmt(stack['stacked'])} dropped={_fmt(stack['dropped'])} "
                    f"errcode={_fmt(stack['frame_errcode'])})"
                )
        # No counters run while idle, so a frozen ended View cannot read as a
        # stall; the next session re-baselines in _observe_session.
        self._observing = False
        return texts

    def _observe_session(self, stack: dict) -> list[str]:
        stage, target = stack["stage"], stack["target_name"]
        texts: list[str] = []
        new_session = self._observing is not True or target != self._target
        if new_session:
            self._reset_counters()
        if new_session or stage != self._stage:
            texts.append(
                f"stage={_fmt(stage)} target={_fmt(target)} "
                f"stacked={_fmt(stack['stacked'])} dropped={_fmt(stack['dropped'])}"
            )
        texts.extend(self._count_events(stack))
        self._observing = True
        self._stage = stage
        self._target = target
        self._last_stack = stack
        return texts

    def _count_events(self, stack: dict) -> list[str]:
        stacked, dropped = stack["stacked"], stack["dropped"]
        errcode = stack["frame_errcode"]
        texts: list[str] = []
        if _is_count(dropped):
            if self._dropped is not None and dropped - self._dropped >= self.drop_burst:
                texts.append(
                    f"DROPS +{dropped - self._dropped} in one poll "
                    f"(stacked={_fmt(stacked)} dropped={dropped} errcode={_fmt(errcode)})"
                )
            self._dropped = dropped
        if _is_count(stacked):
            idx = stacked // self.milestone
            if self._milestone_idx is not None and idx > self._milestone_idx:
                texts.append(f"milestone stacked={stacked} dropped={_fmt(dropped)}")
            self._milestone_idx = idx
            # A stall is flat `stacked` WHILE STACKING: an Initialise or goto
            # phase holds the count still by design.
            if stack["stage"] == "Stack" and stacked == self._stacked:
                self._flat += 1
                if self._flat == self.stall_polls:
                    texts.append(
                        f"STALL: stacked flat at {stacked} for {self._flat} polls "
                        f"(dropped={_fmt(dropped)} errcode={_fmt(errcode)})"
                    )
            else:
                self._flat = 0
            self._stacked = stacked
        return texts


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def watch(
    poll: Callable[[], Awaitable[Any]],
    watcher: SlotWatcher,
    *,
    duration_s: float,
    every_s: float,
    emit: Callable[[str], Any],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    utcnow: Callable[[], datetime] = _utcnow,
) -> None:
    """Poll every ``every_s`` for ``duration_s``, emitting each event line.

    A failed poll becomes an ERR line and the watch carries on; only the end of
    the window stops it. A zero-length window never polls.
    """
    if not every_s > 0:
        raise ValueError(f"every_s must be > 0, got {every_s!r}")
    start = clock()
    deadline = start + duration_s
    next_poll = start
    while clock() < deadline:
        try:
            lines = watcher.observe(await poll(), utcnow())
        except Exception as exc:  # noqa: BLE001 - a failed poll is an event, not the end
            lines = watcher.observe_error(exc, utcnow())
        for line in lines:
            emit(line)
        # Keep the cadence: a poll that overran its slot skips the missed
        # ticks instead of firing catch-up reads at the device back to back.
        while next_poll <= clock():
            next_poll += every_s
        pause = min(next_poll, deadline) - clock()
        if pause > 0:
            await sleep(pause)
    emit(_one_line(_stamp(utcnow()), _END_LINE))


def _at_least(minimum: float, kind: type) -> Callable[[str], Any]:
    def parse(text: str) -> Any:
        value = kind(text)
        # NaN compares false with everything, so it would slip past `<` alone
        # and turn the poll loop's arithmetic into a busy loop.
        if not math.isfinite(value) or value < minimum:
            raise argparse.ArgumentTypeError(f"must be a finite number >= {minimum:g}")
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m seestar_mcp.slot_watch",
        description=(
            "Poll get_view_state and print one UTF-8 line per stacking event: "
            "stage change, drop burst, stall, session end, error, milestone."
        ),
    )
    parser.add_argument(
        "--duration", type=_at_least(0, float), default=1740.0,
        help="seconds to watch, then exit 0 (default 1740: 29 min, inside a "
        "Monitor's 30-min cap)",
    )
    parser.add_argument(
        "--every", type=_at_least(_MIN_EVERY_S, float), default=60.0,
        help=f"seconds between polls (default 60, minimum {_MIN_EVERY_S:g})",
    )
    parser.add_argument(
        "--drop-burst", type=_at_least(1, int), default=3,
        help="report when at least this many frames drop in one poll (default 3)",
    )
    parser.add_argument(
        "--milestone", type=_at_least(1, int), default=60,
        help="report every this many stacked frames (default 60)",
    )
    parser.add_argument(
        "--stall-polls", type=_at_least(1, int), default=3,
        help="report a stall after this many polls with stacked flat in Stack "
        "(default 3)",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _build_client() -> AlpacaClient:
    """Settings to AlpacaClient, wired as ``SeestarController.from_settings`` does.

    Only the Alpaca client is built. The watcher has no use for the data client,
    which reads the image share, or for the tier-1 monitor.
    """
    settings = get_settings()
    client_id = f"{settings.client_id}/{_CLIENT_ID}" if settings.client_id else _CLIENT_ID
    provenance = ProvenanceLog(settings.provenance_log, client_id=client_id)
    return AlpacaClient.from_settings(settings, provenance)


def _emit(line: str) -> None:
    print(line, flush=True)


def _utf8_stdout() -> None:
    """Emit UTF-8 whatever the console code page: target names can be non-ASCII,
    and cp1252 on Windows would raise on them instead of printing."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - keep the stream as it is
            pass


async def _run(client: AlpacaClient, args: argparse.Namespace) -> None:
    watcher = SlotWatcher(
        drop_burst=args.drop_burst,
        milestone=args.milestone,
        stall_polls=args.stall_polls,
    )
    try:
        await watch(
            lambda: client.method_sync("get_view_state"),
            watcher,
            duration_s=args.duration,
            every_s=args.every,
            emit=_emit,
        )
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> int:
    """Run one watch window. Exit 0 at its end; exit 2 if it cannot start."""
    args = parse_args(argv)
    _utf8_stdout()
    try:
        client = _build_client()
    except Exception as exc:  # noqa: BLE001 - only stdout reaches a Monitor
        _emit(_one_line(_stamp(_utcnow()), f"ERR cannot start: {_clip(_describe(exc))}"))
        return 2
    asyncio.run(_run(client, args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
