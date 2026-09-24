"""Unit tests for seestar_mcp.slot_watch -- the shipped slot watcher (task 6).

Live test 2026-09-24: a quiet once-a-minute ``get_view_state`` event stream kept
a whole night observable without noise. These tests pin its event semantics
over scripted sequences shaped like the captured payloads in
``tests/test_server.py``: one line per stage change, drop burst, stall, session
end (``observing`` turning false), error, and every N stacked frames -- and
nothing at all on a quiet poll. No network, no real sleeping.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from seestar_mcp.slot_watch import SlotWatcher, main, parse_args, watch
from tests.test_server import (
    FRESH_BOOT_VIEW_STATE,
    PARKED_ENDED_VIEW_STATE,
    REAL_VIEW_STATE_STACKING,
)

T0 = datetime(2026, 9, 20, 1, 0, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _view(
    *,
    stacked,
    dropped,
    stage="Stack",
    target="M57",
    state="working",
    mode="star",
    errcode=530,
):
    """The captured stacking payload with the counters under test swapped in."""
    reply = copy.deepcopy(REAL_VIEW_STATE_STACKING)
    view = reply["result"]["View"]
    view.update(state=state, mode=mode, stage=stage, target_name=target)
    view["Stack"].update(
        stacked_frame=stacked, dropped_frame=dropped, frame_errcode=errcode
    )
    return reply


def _initialising(target="M57"):
    """An Initialise-phase View: working, but no Stack block yet."""
    return {
        "result": {
            "View": {
                "state": "working",
                "mode": "star",
                "stage": "Initialise",
                "target_name": target,
            }
        },
        "code": 0,
    }


# --- baseline and quiet polls ------------------------------------------------


def test_first_poll_while_stacking_emits_one_baseline_line():
    lines = SlotWatcher().observe(REAL_VIEW_STATE_STACKING, at(0))

    assert lines == ["01:00:00Z stage=Stack target=M57 stacked=3 dropped=7"]


def test_quiet_poll_emits_nothing():
    w = SlotWatcher()
    w.observe(_view(stacked=3, dropped=7), at(0))

    assert w.observe(_view(stacked=9, dropped=8), at(1)) == []
    assert w.observe(_view(stacked=15, dropped=9), at(2)) == []


def test_first_poll_on_a_parked_scope_reports_idle_once_then_stays_quiet():
    """A parked scope keeps the ended View (AMENDED task 5 finding) -- that is
    idle, not a session ending under the watcher, and its frozen counts must
    not read as a stall."""
    w = SlotWatcher()

    assert w.observe(PARKED_ENDED_VIEW_STATE, at(0)) == [
        "01:00:00Z idle: no session running "
        "(view_state=cancel target=M1 stacked=1003 dropped=41 errcode=266)"
    ]
    for minute in range(1, 6):
        assert w.observe(PARKED_ENDED_VIEW_STATE, at(minute)) == []


def test_first_poll_on_a_fresh_boot_reports_no_view():
    assert SlotWatcher().observe(FRESH_BOOT_VIEW_STATE, at(0)) == [
        "01:00:00Z idle: no view since boot (result is empty)"
    ]


# --- stage change --------------------------------------------------------------


def test_stage_change_emits_one_line():
    w = SlotWatcher()
    assert w.observe(_initialising(), at(0)) == [
        "01:00:00Z stage=Initialise target=M57 stacked=- dropped=-"
    ]
    assert w.observe(_initialising(), at(1)) == []

    assert w.observe(_view(stacked=0, dropped=0), at(2)) == [
        "01:02:00Z stage=Stack target=M57 stacked=0 dropped=0"
    ]


# --- drop burst ----------------------------------------------------------------


def test_drop_burst_fires_at_the_threshold_and_not_below():
    w = SlotWatcher()
    w.observe(_view(stacked=10, dropped=2), at(0))

    assert w.observe(_view(stacked=14, dropped=4), at(1)) == []  # +2 < 3
    assert w.observe(_view(stacked=16, dropped=7), at(2)) == [
        "01:02:00Z DROPS +3 in one poll (stacked=16 dropped=7 errcode=530)"
    ]


def test_drop_burst_threshold_is_configurable():
    w = SlotWatcher(drop_burst=5)
    w.observe(_view(stacked=10, dropped=0), at(0))

    assert w.observe(_view(stacked=12, dropped=4), at(1)) == []
    assert w.observe(_view(stacked=13, dropped=9), at(2)) == [
        "01:02:00Z DROPS +5 in one poll (stacked=13 dropped=9 errcode=530)"
    ]


# --- milestone -----------------------------------------------------------------


def test_milestone_fires_each_time_stacked_crosses_a_multiple():
    w = SlotWatcher()
    # Arming mid-session at 115 frames is a baseline, not a milestone.
    assert w.observe(_view(stacked=115, dropped=0), at(0)) == [
        "01:00:00Z stage=Stack target=M57 stacked=115 dropped=0"
    ]

    assert w.observe(_view(stacked=121, dropped=0), at(1)) == [
        "01:01:00Z milestone stacked=121 dropped=0"
    ]
    assert w.observe(_view(stacked=127, dropped=0), at(2)) == []


def test_milestone_size_is_configurable():
    w = SlotWatcher(milestone=10)
    w.observe(_view(stacked=5, dropped=0), at(0))

    assert w.observe(_view(stacked=11, dropped=0), at(1)) == [
        "01:01:00Z milestone stacked=11 dropped=0"
    ]


# --- stall ---------------------------------------------------------------------


def test_stall_fires_once_after_three_flat_polls_in_stack():
    w = SlotWatcher()
    w.observe(_view(stacked=20, dropped=1), at(0))

    assert w.observe(_view(stacked=20, dropped=1), at(1)) == []
    assert w.observe(_view(stacked=20, dropped=2), at(2)) == []
    assert w.observe(_view(stacked=20, dropped=2), at(3)) == [
        "01:03:00Z STALL: stacked flat at 20 for 3 polls (dropped=2 errcode=530)"
    ]
    assert w.observe(_view(stacked=20, dropped=2), at(4)) == []  # reported once

    # Progress resets the count; a fresh stall needs three more flat polls.
    assert w.observe(_view(stacked=21, dropped=2), at(5)) == []
    assert w.observe(_view(stacked=21, dropped=2), at(6)) == []
    assert w.observe(_view(stacked=21, dropped=2), at(7)) == []
    assert w.observe(_view(stacked=21, dropped=2), at(8)) == [
        "01:08:00Z STALL: stacked flat at 21 for 3 polls (dropped=2 errcode=530)"
    ]


def test_stall_is_not_counted_outside_stage_stack():
    w = SlotWatcher()
    w.observe(_view(stacked=20, dropped=1, stage="Initialise"), at(0))

    for minute in range(1, 6):
        assert w.observe(_view(stacked=20, dropped=1, stage="Initialise"), at(minute)) == []


def test_stall_polls_is_configurable():
    w = SlotWatcher(stall_polls=2)
    w.observe(_view(stacked=20, dropped=1), at(0))

    assert w.observe(_view(stacked=20, dropped=1), at(1)) == []
    assert w.observe(_view(stacked=20, dropped=1), at(2)) == [
        "01:02:00Z STALL: stacked flat at 20 for 2 polls (dropped=1 errcode=530)"
    ]


# --- session ended (observing turning false) ------------------------------------


def test_session_ending_emits_one_line_with_the_final_counts_and_errcode():
    """AMENDED (task 6): idle means ``observing`` turning false. A parked or
    ended session reads View.state "cancel", not ``result: {}``."""
    w = SlotWatcher()
    w.observe(_view(stacked=1000, dropped=41, target="M1", errcode=266), at(0))

    assert w.observe(PARKED_ENDED_VIEW_STATE, at(1)) == [
        "01:01:00Z ENDED: session ended (view_state=cancel) "
        "target=M1 stacked=1003 dropped=41 errcode=266"
    ]
    assert w.observe(PARKED_ENDED_VIEW_STATE, at(2)) == []


def test_view_vanishing_mid_session_ends_it_with_the_last_seen_counts():
    w = SlotWatcher()
    w.observe(_view(stacked=40, dropped=3), at(0))

    assert w.observe(FRESH_BOOT_VIEW_STATE, at(1)) == [
        "01:01:00Z ENDED: session ended (view gone; last seen) "
        "target=M57 stacked=40 dropped=3 errcode=530"
    ]
    assert w.observe(FRESH_BOOT_VIEW_STATE, at(2)) == []


def test_new_session_after_an_end_is_announced_and_rebaselined():
    """Same target re-acquired (e.g. LP filter off after sustained drops)."""
    w = SlotWatcher()
    w.observe(_view(stacked=300, dropped=160), at(0))
    w.observe(_view(stacked=301, dropped=160, state="cancel", mode="none"), at(1))

    assert w.observe(_view(stacked=4, dropped=0), at(4)) == [
        "01:04:00Z stage=Stack target=M57 stacked=4 dropped=0"
    ]
    assert w.observe(_view(stacked=10, dropped=1), at(5)) == []


# --- same-target restart (stacked decreasing) --------------------------------------
# Final review G11 (2026-09-24): a same-target re-acquire (the LP-filter branch's
# broadband re-acquire) can land between two polls without one ever reading
# ``observing`` false. A stack only counts up, so a decrease is a new stack --
# the same rule qa_tier1 uses (task 3) -- and it was not announced.


def test_stacked_decrease_on_the_same_target_is_announced_as_a_new_stack():
    w = SlotWatcher(milestone=10)
    w.observe(_view(stacked=300, dropped=160), at(0))
    w.observe(_view(stacked=300, dropped=161), at(1))  # flat x1 on the old stack

    assert w.observe(_view(stacked=4, dropped=0), at(2)) == [
        "01:02:00Z new stack on M57 (stacked 300→4)"
    ]
    # Milestones count from the new stack's own baseline.
    assert w.observe(_view(stacked=9, dropped=0), at(3)) == []
    assert w.observe(_view(stacked=11, dropped=1), at(4)) == [
        "01:04:00Z milestone stacked=11 dropped=1"
    ]
    # Drops are measured from the new stack's baseline, not the old 161.
    assert w.observe(_view(stacked=13, dropped=4), at(5)) == [
        "01:05:00Z DROPS +3 in one poll (stacked=13 dropped=4 errcode=530)"
    ]
    # The old stack's flat poll does not carry over: a stall needs three new ones.
    assert w.observe(_view(stacked=13, dropped=4), at(6)) == []
    assert w.observe(_view(stacked=13, dropped=4), at(7)) == []
    assert w.observe(_view(stacked=13, dropped=4), at(8)) == [
        "01:08:00Z STALL: stacked flat at 13 for 3 polls (dropped=4 errcode=530)"
    ]


def test_stacked_decrease_across_an_acquisition_phase_is_still_a_new_stack():
    """The re-acquire's Initialise poll carries no Stack block; the decrease is
    measured against the last count seen, so it is still caught."""
    w = SlotWatcher()
    w.observe(_view(stacked=300, dropped=160), at(0))
    assert w.observe(_initialising(), at(1)) == [
        "01:01:00Z stage=Initialise target=M57 stacked=- dropped=-"
    ]

    assert w.observe(_view(stacked=2, dropped=0), at(3)) == [
        "01:03:00Z new stack on M57 (stacked 300→2)",
        "01:03:00Z stage=Stack target=M57 stacked=2 dropped=0",
    ]


# --- target change ---------------------------------------------------------------


def test_new_target_resets_the_drop_and_milestone_baselines():
    """A target switched between polls: M1's counts are its own, not deltas
    against M57's (else DROPS +15 and a milestone would fire here)."""
    w = SlotWatcher()
    w.observe(_view(stacked=110, dropped=5, target="M57"), at(0))

    assert w.observe(_view(stacked=130, dropped=20, target="M1"), at(1)) == [
        "01:01:00Z stage=Stack target=M1 stacked=130 dropped=20"
    ]
    assert w.observe(_view(stacked=136, dropped=21, target="M1"), at(2)) == []


def test_new_target_resets_the_stall_counter():
    w = SlotWatcher()
    w.observe(_view(stacked=12, dropped=0, target="M57"), at(0))
    w.observe(_view(stacked=12, dropped=0, target="M57"), at(1))
    w.observe(_view(stacked=12, dropped=0, target="M57"), at(2))  # M57 flat x2

    # Same count by coincidence on the new target: not a third flat poll.
    assert w.observe(_view(stacked=12, dropped=0, target="M1"), at(3)) == [
        "01:03:00Z stage=Stack target=M1 stacked=12 dropped=0"
    ]
    assert w.observe(_view(stacked=12, dropped=0, target="M1"), at(4)) == []
    assert w.observe(_view(stacked=12, dropped=0, target="M1"), at(5)) == []
    assert w.observe(_view(stacked=12, dropped=0, target="M1"), at(6)) == [
        "01:06:00Z STALL: stacked flat at 12 for 3 polls (dropped=0 errcode=530)"
    ]


# --- errors ------------------------------------------------------------------------


def test_native_error_reply_is_an_err_line_not_an_ended_session():
    """A failed read must not masquerade as ``observing`` turning false."""
    w = SlotWatcher()
    w.observe(_view(stacked=30, dropped=2), at(0))

    assert w.observe({"error": "method not found", "code": 103}, at(1)) == [
        "01:01:00Z ERR get_view_state: method not found (code 103)"
    ]
    assert w.observe("Error: Exceeded allotted wait time for result", at(2)) == [
        "01:02:00Z ERR get_view_state: Error: Exceeded allotted wait time for result"
    ]
    # The session is still running and the counters were not disturbed.
    assert w.observe(_view(stacked=36, dropped=3), at(3)) == []


@pytest.mark.parametrize("junk", [None, 123, [], {}, {"result": "junk"}])
def test_malformed_reply_is_an_err_line_and_never_raises(junk):
    lines = SlotWatcher().observe(junk, at(0))

    assert len(lines) == 1
    assert lines[0].startswith("01:00:00Z ERR unexpected get_view_state reply: ")


def test_observe_error_is_one_err_line_even_for_a_multiline_message():
    lines = SlotWatcher().observe_error(TimeoutError("read timed out\nretrying"), at(4))

    assert lines == ["01:04:00Z ERR poll failed: TimeoutError: read timed out retrying"]


def test_timestamp_is_utc_whatever_the_clock_hands_in():
    naive = SlotWatcher().observe(REAL_VIEW_STATE_STACKING, datetime(2026, 9, 20, 1, 0, 0))
    plus_two = SlotWatcher().observe(
        REAL_VIEW_STATE_STACKING,
        datetime(2026, 9, 20, 3, 0, 0, tzinfo=timezone(timedelta(hours=2))),
    )

    assert naive[0].startswith("01:00:00Z ")
    assert plus_two[0].startswith("01:00:00Z ")


# --- the polling loop ----------------------------------------------------------------


class _FakeClock:
    """Monotonic clock + sleep that advance together; nothing really sleeps."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


async def test_watch_polls_on_cadence_and_survives_a_failed_poll():
    clock = _FakeClock()
    replies = iter(
        [REAL_VIEW_STATE_STACKING, RuntimeError("bridge down"), _view(stacked=9, dropped=8)]
    )
    polled_at: list[float] = []

    async def poll():
        polled_at.append(clock.t)
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    out: list[str] = []
    await watch(
        poll,
        SlotWatcher(),
        duration_s=180,
        every_s=60,
        emit=out.append,
        clock=clock,
        sleep=clock.sleep,
        utcnow=lambda: T0 + timedelta(seconds=clock.t),
    )

    assert polled_at == [0, 60, 120]
    assert out == [
        "01:00:00Z stage=Stack target=M57 stacked=3 dropped=7",
        "01:01:00Z ERR poll failed: RuntimeError: bridge down",
        "01:03:00Z watch window ended (re-arm to keep watching)",
    ]


async def test_watch_with_zero_duration_never_polls():
    clock = _FakeClock()

    async def poll():
        raise AssertionError("a zero-length window must not touch the device")

    out: list[str] = []
    await watch(
        poll,
        SlotWatcher(),
        duration_s=0,
        every_s=60,
        emit=out.append,
        clock=clock,
        sleep=clock.sleep,
        utcnow=lambda: T0,
    )

    assert out == ["01:00:00Z watch window ended (re-arm to keep watching)"]


# --- CLI -----------------------------------------------------------------------------


def test_parse_args_defaults_match_the_documented_command():
    args = parse_args([])

    assert (args.duration, args.every, args.drop_burst, args.milestone, args.stall_polls) == (
        1740,
        60,
        3,
        60,
        3,
    )


def test_parse_args_reads_every_flag():
    args = parse_args(
        [
            "--duration", "600",
            "--every", "30",
            "--drop-burst", "5",
            "--milestone", "100",
            "--stall-polls", "4",
        ]
    )

    assert (args.duration, args.every, args.drop_burst, args.milestone, args.stall_polls) == (
        600,
        30,
        5,
        100,
        4,
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--every", "0"],
        ["--every", "5"],  # below the device-touching floor
        ["--duration", "-1"],
        ["--drop-burst", "0"],
        ["--milestone", "0"],
        ["--stall-polls", "0"],
    ],
)
def test_parse_args_rejects_values_that_hammer_the_device_or_cannot_fire(argv):
    with pytest.raises(SystemExit):
        parse_args(argv)


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False

    async def method_sync(self, method, params=None):
        self.calls.append(method)
        return REAL_VIEW_STATE_STACKING

    async def aclose(self) -> None:
        self.closed = True


def test_main_with_zero_duration_exits_zero_without_polling(monkeypatch, capsys):
    import seestar_mcp.slot_watch as slot_watch

    client = _FakeClient()
    monkeypatch.setattr(slot_watch, "_build_client", lambda: client)

    assert main(["--duration", "0"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    assert out[0].endswith("Z watch window ended (re-arm to keep watching)")
    assert client.calls == []
    assert client.closed


def test_main_polls_only_get_view_state_and_closes_the_client(monkeypatch, capsys):
    """The watcher's whole device footprint: one native read per --every."""
    import seestar_mcp.slot_watch as slot_watch

    client = _FakeClient()
    monkeypatch.setattr(slot_watch, "_build_client", lambda: client)
    clock = _FakeClock()
    real_watch = slot_watch.watch

    async def fake_time_watch(poll, watcher, **kwargs):
        await real_watch(
            poll, watcher, clock=clock, sleep=clock.sleep, utcnow=lambda: T0, **kwargs
        )

    monkeypatch.setattr(slot_watch, "watch", fake_time_watch)

    assert main(["--duration", "180", "--every", "60"]) == 0

    assert client.calls == ["get_view_state"] * 3
    assert client.closed
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "01:00:00Z stage=Stack target=M57 stacked=3 dropped=7",
        "01:00:00Z watch window ended (re-arm to keep watching)",
    ]


def test_main_reports_a_startup_failure_on_stdout(monkeypatch, capsys):
    """Only stdout reaches a Monitor, so a stderr-only traceback would be
    silence -- and silence reads as 'nothing happened'."""
    import seestar_mcp.slot_watch as slot_watch

    def broken():
        raise ValueError("bad SEESTAR_ALPACA_BASE_URL")

    monkeypatch.setattr(slot_watch, "_build_client", broken)

    assert main(["--duration", "0"]) == 2

    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    assert "ERR cannot start: ValueError: bad SEESTAR_ALPACA_BASE_URL" in out[0]


# --- the shared parser ------------------------------------------------------------------


def test_server_reuses_the_shared_view_state_parser():
    """One read of ``observing`` for the tool and the watcher, never two."""
    from seestar_mcp import native_reply, server

    assert server._summarize_view_state is native_reply._summarize_view_state
    assert server._native_error is native_reply._native_error


def test_slot_watch_does_not_import_the_mcp_server():
    """Importing server.py builds FastMCP (which reconfigures root logging) and
    pulls in astropy/photutils: ~5 s on the dev PC against ~0.7 s without."""
    code = "import sys, seestar_mcp.slot_watch; print('seestar_mcp.server' in sys.modules)"
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )

    assert done.stdout.strip() == "False"
