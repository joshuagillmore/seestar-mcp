---
name: autonomous-night
description: >
  Unattended full-night run-book for a Seestar S50: assess, run the ranked plan
  target-by-target, react to conditions/QA, and wind down + park at dawn — on its own,
  behind hard safety guardrails and a mandatory dry-run confirmation. Use whenever the
  user wants to hand over the whole night — e.g. "run the whole night", "image
  unattended", "run an autonomous session", "image all night on your own", "run my
  target list unattended", "let it run itself till dawn". Starts with a NO-MOTION
  `simulate_night` projection that the user must explicitly approve before the first
  motion command. Orchestrates the existing tools in a visible loop — it decides
  *whether to keep going and what's next*; it does not re-implement motion (that is
  `run-session`).
---

# Seestar S50 Autonomous Night

This skill runs a whole night unattended: propose a plan, get one explicit go-ahead,
then loop target-by-target under hard guardrails, and park at dawn or on any hard stop.
Autonomy here is **Claude driving the existing audited tools in a visible loop** — not a
hidden background engine. Every decision is logged and surfaced. When in doubt, **stop
and park (fail safe).**

## Operating assumptions
- The user is usually watching from the Claude phone app via Remote Control on a small
  screen. Every message is one line, lead with state, not prose. Notify on each target
  change and every stop — the phone is how they know what happened overnight.
- This skill owns *sequencing and safety*, not motion. Planning judgment lives in
  **`observing-planner`**; execution (goto → focus → stack → monitor) lives in
  **`run-session`**; faults live in **`anomaly-playbook`**; QA thresholds live in
  **`qa-policy`**. Do not re-implement any of those here.
- Provenance: the tools log their calls; you add a one-line human-readable note per
  guardrail decision and target switch.

## Phase A0 — Arm the watch (MANDATORY, before the dry run)

See **`run-session` Phase 0** for the full rationale; it is required here too and
matters more, because "unattended" is the whole point of this skill.

Before proposing anything, arm both:

1. **A time-driven heartbeat** — a recurring wakeup roughly every 15 minutes for
   the whole dark window. An agent only exists while something invokes it; an
   event `Monitor` is silent when nothing is wrong and therefore leaves the run
   unsupervised through every *normal* decision (goal reached, altitude floor,
   scheduled switch). A Monitor supplements the heartbeat; it never replaces it.
   Use the shipped slot watcher (`python -m seestar_mcp.slot_watch`) as the
   Monitor's source; the command and its event lines are in `run-session` Phase 0.
2. **A detached park watchdog** — use the shipped
   **`deploy/dawn_park_watchdog.sh`**; do not hand-roll one:

   ```bash
   nohup deploy/dawn_park_watchdog.sh "2026-08-04 07:40:00" /tmp/wd.log >/dev/null 2>&1 &
   ```

   Plain HTTP, no MCP and no agent, so it survives the session ending.

**An autonomous night without a heartbeat is not autonomous — it is a single
goto followed by silence.** On 2026-08-03/04 exactly that happened: the planned
target switch never fired because nothing invoked the agent, and the only thing
that behaved correctly overnight was the detached watchdog.

If you cannot arm a heartbeat, say so before the run and treat the night as
single-target: one acquisition plus the watchdog park. Do not present a
multi-target schedule you have no mechanism to execute.

## Phase A — Propose (NO MOTION, mandatory confirmation gate)
1. `simulate_night` (optionally pass `types` / `limit` if the user asked). This is a
   **dry run — it issues NO motion.** It returns the conditions verdict, the dark
   window, and the ordered projected schedule.
2. **Reconcile the schedule against the CURRENT time.** `simulate_night` packs from the
   start of the dark window, not from now — so if the night is already underway (the common
   case), its early slots are already in the past and the list is not runnable as returned.
   Drop the slots that have passed and re-pack the remainder from the current time, so you
   present **only what can actually be observed tonight**. Say it in one line, e.g.
   `Dark started 22:57; it's now 00:06 — first two slots already past. Runnable remainder:`
   Never hand the user a schedule whose first target's window has already closed.
3. Present, compactly:
   - the **one-line conditions verdict** (from `simulate_night`'s `conditions`);
   - the **ordered schedule** — per `ScheduledTarget`, one line: name · window (UTC) ·
     minutes · `subs × 10s` · the one-line reason, e.g.
     `1. M27 · 22:40–00:10 UTC · 90 min · 540×10s · long sweet-band pass, suits site.`
   - the **guardrail defaults** that will apply: dawn margin (15 min), battery floor
     (20%), max session (10 h), weather no-go stops.
   - a note that **the first target spends ~2–4 min acquiring** (full alignment +
     autofocus) and **later ones often ~1–2 min** before the first frame stacks, so a
     45-min slot yields roughly 41–44 min of integration. Do not promise the full slot as
     integration time.
4. **State plainly that this is a dry run and REQUIRE explicit user confirmation before
   ANY motion command.** Say it in one line, e.g.
   `Dry run only — nothing has moved. Reply "go" to start the run; I'll park at dawn or on any hard stop.`
   This confirmation gate is **mandatory and non-skippable.** Do not slew, focus, stack,
   or otherwise command motion until the user explicitly says to begin.
5. If conditions are **no-go** (`simulate_night` returns `ok:false`, an empty schedule,
   or a no-go verdict), say so in one line and **do not start.** Offer to re-simulate
   later or for a clearing window, but issue no motion.

## Guardrail semantics (read this before overriding a stop)
Hard stops are **predictive, not reactive.** `check_night_guardrails` reads the forecast and
device health, so a weather stop can fire while the current sky is still stacking cleanly
with zero dropped frames. **That is correct behavior, not a false positive** — the lead time
is exactly what lets the mount stop and fold *before* precipitation or heavy dew arrives.
Two signals commonly drive it:
- **precipitation** forecast inside the session window, and
- **dew risk** — a small temperature/dew-point spread (a couple of °C or less) means
  condensation forming on the optics, which ends the night's usefulness even under a clear
  sky.

When a stop fires: corroborate once with `assess_conditions` to get the human-readable
reason, state it in one line, and **wind down (Phase C).** Do not re-run the check hoping
for a different answer, and never resume a stopped run unless the user explicitly asks.

## Phase B — Loop (per target)
Record the run's `session_start_utc` at first go-ahead. Then, for each target:

1. **Guardrail check FIRST — every iteration, no exceptions.** Call
   `check_night_guardrails(session_start_utc=...)`. **It fails closed: proceed ONLY
   when the result has `ok: true` AND `action: "continue"`.** Anything else —
   `action: "park_and_stop"`, `ok: false` (no site profile, or the check itself
   errored), a missing `action`, or any other value — goes straight to **Phase C**.
   Quote the reason in one line: the hard stop (from `hard_stops` / `reasons`), or the
   `error` when the check could not run. A check that could not run is a stop, never
   a pass. **Never skip this check between targets.**
2. Only on that clean `continue`, take the **next `ScheduledTarget`** from the schedule
   and hand it to the **`run-session`** skill: goto → plate-solve → focus → stack →
   monitor (the `qa_tier1` cadence plus the Phase 4 live reactivity — conditions watch
   and sweet-band watch).
   Notify the user of the target change in one line.
   - **The approved plan is an authorization, not a blank cheque.** The user approved *that
     schedule*. Running it as-is needs no further confirmation, but any **material
     deviation** — skipping a target, substituting one that was not in the dry run,
     reordering, or materially extending a slot — must be **surfaced in one line as it
     happens**, with the reason. Skipping an obstructed target and moving on is fine and
     expected; doing it silently is not. The same holds for re-acquiring the same target
     broadband when the LP filter drops frames under the moon (the anomaly-playbook
     LP-filter branch): no fresh confirmation, one line as it happens. Anything that
     would take the night somewhere the user did not see in the dry run (a target
     off-plan, the dew heater, a mask edit) needs a fresh confirmation.
   - **When a target is blocked, check its neighbours before slewing.** A local obstruction
     is a *direction*, not a single target: before taking the next item, scan the remaining
     plan for targets at similar azimuth and **lower** altitude and skip them together.
     Discovering the same roofline one slew at a time can waste most of an hour.
3. **End the target's slot** when any of these happen: its scheduled window ends, it
   leaves its sweet band (nearing the field-rotation ceiling or the altitude floor), or
   QA collapses. Then call `log_session_result(...)` for it (integration, sub counts,
   median FWHM per the wind-down in `run-session`) and **re-enter the loop** at step 1
   for the next target.
4. **Faults → `anomaly-playbook`.** Route any mid-target fault (stall, solve/focus
   failure, tracking loss, connection drop, weather flip) there. If it resolves, resume
   the loop. If it is an **unrecoverable fault or a hard guardrail stop**, go to Phase C
   — end in `park`. Re-check guardrails on any anomaly, not just at slot boundaries.
   The Phase A go-ahead already authorised that park: a hard stop or a confirmed weather
   no-go (`assess_conditions` returned `go: false`) parks **without asking** (the
   playbook's precedence rule) — never block on a question nobody is awake to answer.
   Cloud rising while `go` is still `true` (or `null`) is not a no-go and does not end
   the night on its own.

## Phase C — Wind down + park
Reached on any hard stop, unrecoverable fault, end of schedule, or user stop. Fold the
mount before the bookkeeping: logging is local and can wait, the weather cannot.
1. `stop_view` to end stacking cleanly. **Go on to `park` whatever it returns** — a
   native error now comes back `ok: false`, and a failed `stop_view` must never stall
   the wind-down.
2. **`park`** the mount (stops tracking, optics to horizontal). Parking is
   non-negotiable on any hard stop.
3. **Confirm the fold — the `park` reply is not proof either way.** Poll `get_status`
   about every 30 s for up to ~4 min until `mount_parked` is `true`. The fold typically
   completes in ~20–30 s (live test 2026-09-24), but ~4 min is the upper bound: a
   shorter poll re-parks a mount that is still folding and raises a false alarm. Read
   `mount_parked` (the device's own `mount.close`), never `tracking` — that
   is Alpaca's view and disagrees with the device on this hardware; `mount_parked: null`
   means the native read failed, which confirms nothing. **A confirmed
   `mount_parked: true` is parked, even if `park` returned `ok: false`.** Only when the
   fold is still **not** confirmed at the end of the poll, **retry `park` once** (a
   repeat `park` on an already-folded scope returns code 0 and is harmless, live test
   2026-09-24) and poll again the same way. If it still fails, **alert the user
   loudly** — a push notification if one is available — and name the backstop, e.g.
   `PARK NOT CONFIRMED — mount may be unfolded. Dawn watchdog parks at 07:40Z as backstop.`
   If `park` returned `ok: false` but the fold was then confirmed, `get_run_state` may
   still read `active` (or `unknown` once its stamp goes stale), because `park` clears
   the run state only on `ok: true`. That is expected: the confirmed fold is what
   counts, so do not treat that state as a live run.
4. `log_session_result(...)` for the **in-progress** target so its integration is not
   lost.
5. **Summarize the night** in a compact block and **notify the user**: targets imaged,
   integration on each, projects advanced, and the reason the run ended (dawn / battery
   / weather / connection / max duration / schedule complete / user stop).
6. Only `shutdown` if the user **pre-authorized** it **and** step 3 confirmed the park
   (shutdown ends the seestar_alp link — and with it the watchdog's backstop).
   Otherwise leave the scope parked and connected.

## Hard rules
- **Arming a time-driven heartbeat AND a detached park watchdog is MANDATORY before
  any motion** (Phase A0). An event Monitor does not satisfy this — it is silent
  exactly when the night's normal decisions come due. Never call an agent-dependent
  step "scheduled": it is scheduled only if something outside the agent runs it.
- **The dry-run + explicit confirmation before motion is MANDATORY and non-skippable.**
  Nothing moves in Phase A. The first motion command only follows an explicit user
  go-ahead.
- **Five HARD stops, each of which ALWAYS ends in `park`:** astronomical **dawn**
  (within the margin), **low battery** (below floor), **precipitation / hard weather
  no-go**, **lost connection / unverified scope**, **max session duration** exceeded.
  Hard stops are non-negotiable.
- **Never skip `check_night_guardrails` between targets** — call it at the top of every
  loop iteration and on any anomaly.
- **Log every session** (`log_session_result`), including the in-progress target at
  wind-down, so integration accumulates across nights.
- **Keep the user notified** of each target change and every stop (Remote Control
  surfaces these on the phone).
- **Fail safe: when in doubt, stop and park.** If scope health can't be confirmed
  (`check_night_guardrails` can't read device state → treated as disconnected), the run
  stops and parks. A guardrail call that fails outright (`ok: false`) is a stop too
  (Phase B step 1), and a park counts only once `get_status.mount_parked` is `true`
  (Phase C step 3). Never leave the mount slewed or tracking on a fault.
- **This skill decides *whether to keep going and what's next*; it does not re-implement
  motion** (that is `run-session`). Planning = `observing-planner`, execution =
  `run-session`, faults = `anomaly-playbook`, QA = `qa-policy`.

## Operating notes
Non-obvious behaviors that cost real observing time when ignored.
- **`goto_target` returns ok even when the mount does NOT slew, and a normal alignment sits
  in `Initialise` for minutes.** Use the acquisition discriminator in **`run-session`**
  (Phase 1) to tell a healthy alignment from an unsolvable field; skip obstructed targets
  rather than waiting them out. Prefer high, unobstructed targets when the horizon is
  cluttered.
- **PARK strands the pointing model.** `park` points the optics at the cradle; the firmware
  can't plate-solve from there, so every subsequent goto silently fails to slew. **Park
  ONLY at wind-down (Phase C).** To pause/resume mid-night use `stop_view`, never `park`.
  Recovering a mid-session park needs a full re-alignment — a power-cycle, after which the
  first goto runs a 3-point `Initialise` alignment (takes a few minutes).
- **Confirm framing with a real image, not telemetry.** Once per target (early), check the
  object is in frame, focused, and cloud-free — cheaply via the live plate-solve annotation
  (`get_view_state` → `stack.target_px` + `stack.target_radius_px`), or the newest sub JPG
  from the scope's share. Not from `plate_solve`'s `ra_deg`/`dec_deg`: they are the field
  centre in JNow, and only `ra_j2000_deg`/`dec_j2000_deg` compare with the catalog
  (corrected 2026-09-24: the old "near the commanded target" reading was J2000-vs-JNow
  precession). Frame counts don't prove the object is in frame.
  If it is off-centre, **classify the offset** with the procedure in **`run-session`**
  ("Visual framing check") before reacting — do not assume it is systematic, and do not
  burn a slot re-centring one that is.
- **High-latitude / short nights:** astronomical dark can be short, and at high latitude in
  summer there may be no true darkness before sunrise. Put **broadband** targets in the real
  dark and **LP/dual-band nebulae** into twilight — the dual-band tolerates the brightening
  sky far better. Expect the drop rate to climb sharply toward dawn; that's the natural end
  of the useful night, not a fault to chase.
- **Coordinates:** pass catalog **J2000 degrees** to `goto_target` — it precesses them to
  JNow and converts RA to the firmware's hours internally. Don't pre-convert to hours or
  pre-precess to JNow (either one is then applied twice).
- **Never run a file offload off the scope's share during the run.** Heavy transfers compete
  with the scope's control link and can starve it — the run may stall, or the bridge may fail
  to authenticate. Do offloads before the run or after wind-down only.
- **A target dropping EVERYTHING ≠ end of night.** Distinguish a *local* block (a low
  bearing over a roofline or tree line, or a drifting cloud bank) from a *global* one (dew /
  widespread cloud) by slewing to a high target in a different direction: if it stacks clean,
  skip the blocked spot and keep going; only wind down if the whole sky is bad. Don't abandon
  the night on one clouded patch.
- **Pacing a long slot.** A 45-minute slot does not need minute-by-minute polling, and tight
  loops cost more than they reveal. Sample the stack counters on a slow cadence (~1 min) and
  watch for four exit conditions: the **slot boundary**, a **drop-spike** (dropped frames
  climbing sharply across consecutive samples — a cloud bank or something drifting into the
  light path), a **link fault** (repeated solve failures or connection errors), or a
  **guardrail stop**. Any of those warrants a decision; between them, stay quiet. The
  shipped slot watcher (`run-session` Phase 0) does this sampling for you and prints only
  the `DROPS`, `STALL`, `ENDED` and `ERR` lines worth a look.
- **Keep the tool link warm.** A long slot watched by something other than MCP tools means a
  quiet connection, and quiet connections get dropped — a client polling continuously lived
  8.8 h while sessions with 40–60 min silences died repeatedly. Call a **local-only** tool
  (`get_site_profile`, `list_projects`, `get_run_state`) every ~5 min while a slot runs. They
  read local files and cost the device nothing. The failure this prevents is arriving at a
  target boundary with no working tool link.
- **Check `get_run_state` after any interruption.** It answers "is a run in progress?"
  definitively rather than by inferring from a `get_view_state` timeout — `active`, `idle`, or
  `unknown` (a run was recorded but its stamp is stale, so the writer probably died). Treat
  `unknown` as "find out", never as "the scope is free". The scope's own answer is
  `get_view_state.observing`. A parked scope keeps the ended session's View, so neither
  "a View is present" nor `result: {}` is the test.
- **Guardrails inside the slot, not just between targets.** Re-run
  `check_night_guardrails` on a slow cadence (~10 min) *during* a slot as well as at each
  boundary. Weather, dew, and battery do not wait for a slot to end, and a 45-minute slot
  checked only at its edges is 45 minutes unguarded — which defeats the lead time the
  predictive stops exist to give you. Read each in-slot result the same fail-closed way
  as Phase B step 1.
