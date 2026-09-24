---
name: run-session
description: >
  Run-book for executing a Seestar S50 imaging session via the seestar-mcp tools.
  Use whenever the user wants to start, monitor, or wind down an imaging session —
  e.g. "image the Veil Nebula tonight", "start a session on M31", "slew to NGC 7000
  and start stacking", "wrap up the session and pull the subs". Covers pre-flight
  checks, target acquisition, autofocus, stacking, in-session monitoring cadence,
  and clean shutdown. Defers QA scoring decisions to the qa-policy skill and fault
  response to the anomaly-playbook skill.
---

# Seestar S50 Session Run-Book

This skill governs how to run an imaging session end to end using the `seestar-mcp`
tools. Follow the phases in order. Do not skip pre-flight. Treat every motion command
(goto, autofocus, park) as state-changing and confirm it succeeded before proceeding:
`ok: true` first — every command tool returns `ok: false` when the scope's native reply
carries `"error"` with a NONZERO (or missing) `code`, e.g. `"method not found (code 103)"`,
or when seestar_alp hands back an `"Error: ..."` string. `"error"` text with `code: 0` is
a success: `ok: true`, with that text in `warning` (the dew heater answers every toggle
this way on fw 8.46 and still applies it, live test 2026-09-24). Then check the command's
own signal: `get_view_state` progress for a goto (Phase 1), the focuser position for
autofocus (Phase 2), `get_status.mount_parked == true` for a park. Mention a non-null
`warning` in the status line and judge the command by that same signal, never by the
warning text. The heater's own flag (`setting.heater_enable` in the native
`get_device_state`) is not returned by any tool yet, so report a heater toggle as
commanded, not confirmed.

## Operating assumptions
- `seestar-mcp` is already registered and the Claude Code session is running on the
  host near the scope (a small always-on machine is ideal for unattended runs; a laptop that
  sleeps will kill the session). seestar_alp is up on :5555 and the Seestar is on a stable
  LAN IP.
- The user is typically monitoring from the Claude phone app via Remote Control, so
  keep status messages compact and scannable on a small screen. Lead with state, not
  prose. One-line status beats a paragraph.
- "Save each frame in enhancing" should be ON so Tier-2 QA has subs to score. If the
  user has not confirmed this, remind them once at session start; do not nag.

## Phase 0 — Arm the watch (MANDATORY, before anything else)

**Do not slew, stack, or contact the device until both of these are armed.** This
is a hard gate, like the dry-run confirmation in `autonomous-night`.

An agent is **turn-based**: it only exists while something invokes it. A session
left without a heartbeat is not "watching" — it is asleep, and it will stay
asleep through every decision the night needs.

**1. A TIME-DRIVEN heartbeat.** Arm a recurring wakeup (`/loop`, a scheduled
wakeup, or cron) for roughly every **15 minutes across the whole dark window**,
before the first goto. On each wake: re-read altitude against the floor, drop
rate, goal progress, and whether a planned target switch is due.
- **Altitude:** `get_target_observability(target).now`: `alt_deg` at the real
  current instant, with `above_floor` and `in_sweet_band` already judged against
  the site's floor, horizon mask and field-rotation ceiling (`above_floor` true
  but `in_sweet_band` false = above the ceiling). Not its `observability` block,
  which is the whole night's plan. Local-only, so it costs the device nothing. An
  off-catalog target returns `ok: false`; watch its planned window instead.
- **Tracking, once per target while it stacks:** record
  `get_status.mount_tracking` (the device's native flag, not Alpaca's
  `tracking`). Not yet verified mid-stack on fw 8.46, so do not expect it to
  read `true`. Act on it only through the anomaly-playbook branch
  "`mount_tracking` false while stacking". `null` means the read failed; re-read
  on the next wake. `get_status` costs six device reads, so once per target is
  enough.

**2. A DETACHED park watchdog.** A plain script — no MCP, no agent — that talks
to `seestar_alp` over HTTP, and at a fixed dawn deadline stops the view and
parks, exiting quietly if the mount is already folded. It must survive the agent
dying, the session ending, and the client closing. **This is the only layer that
protects the hardware when everything else is gone.**

Use the shipped one — do not hand-roll it:

```bash
nohup deploy/dawn_park_watchdog.sh "2026-08-04 07:40:00" /tmp/wd.log >/dev/null 2>&1 &
```

It reads `SEESTAR_ALPACA_BASE_URL` / `SEESTAR_ALPACA_DEVICE_NUM`, confirms the
fold via the device's own `mount.close` (Alpaca's `/atpark` disagrees on
firmware 7.75 and 8.46), and exits non-zero with a warning if it cannot confirm.

### An event Monitor is NOT a heartbeat

This mistake cost a real night (2026-08-03/04). A `Monitor` is **event-driven**:
it wakes the agent only when something fires. Silence means no invocation. So an
agent "monitoring" via Monitor alone is dormant for as long as nothing goes
wrong — which is most of a good night.

Telescope sessions are exactly where that fails, because the decisions that
matter are **not anomalies**:

- a target reaching its integration goal
- a target sinking toward the altitude floor
- a scheduled target switch coming due
- the moon rising into the field

None of these fire an alert. On that night the agent planned M13 → M34 at
05:05Z, armed a Monitor, and was never invoked again until the park at 07:40Z.
M13 ran ~179 min instead of 85; M34 got nothing. Nothing failed — nobody was
awake. Use a Monitor **in addition to** the heartbeat, never instead of it.

Also tell the Monitor about the planned park time, or the scheduled fold reports
as an "unexpected park" — the only alert that night was that false positive.

**Use the shipped slot watcher as the Monitor's source** — do not hand-roll one.
Run it under a Monitor with `timeout_ms=1800000`:

```bash
PYTHONIOENCODING=utf-8 uv --directory <repo> run python -m seestar_mcp.slot_watch \
    --duration 1740 --every 60 --drop-burst 3 --milestone 60 --stall-polls 3
```

It reads only `get_view_state`, once a minute, and prints one line per event:
a stage or target change, `new stack on <target>` (`stacked` fell on the same
target, e.g. a re-acquire), `DROPS +K`, `milestone`, `STALL`, `ENDED` (the
session's `observing` turned false; carries the final counts and `errcode`) and
`ERR`. Nothing prints while all is well. A Monitor kills its command after at
most 30 min, so the window is 29 min: `watch window ended (re-arm to keep
watching)` means re-arm it. A planned stop or park arrives as an `ENDED` line
like any other, so check it against the plan before calling it unexpected. The
watcher's silence is exactly why it rides alongside the heartbeat, never instead.

### State the guarantee honestly

Before the user goes to bed, say plainly which layer survives you:

> Heartbeat every 15 min while this session lives; a detached watchdog parks at
> 07:40Z regardless. If this session ends, target switches stop — the park does not.

Never describe an agent-dependent step as "scheduled". It is scheduled only if
something outside the agent will run it.

## Phase 0.1 — Pre-flight (always run before goto)
1. `get_status` — confirm the mount is connected and not already slewing. If not
   connected, `connect_telescope` first. **If both fail, work out WHICH link is down before
   touching anything** — they need opposite fixes and this is the most common first-run
   failure:
   - **Transport error / "all connection attempts failed" / connection refused** → the
     *bridge* is not running or not reachable. The scope is not the problem. Start
     `seestar_alp`, wait for it to finish connecting (it can take a minute and may log one
     failed handshake before a retry succeeds), then retry.
   - **Bridge answers but device calls error** (e.g. "not connected", auth failures) → the
     bridge is up and the *scope* is the problem: still booting, asleep, off the network, or
     the firmware-7.18+ handshake failed. See the authentication branch in
     **`anomaly-playbook`**.
   Never report "the telescope is offline" when it is actually the bridge that is down.
2. `get_view_state` — confirm no session is already in progress: read `observing`,
   which is `true` only while a session is actually running. A parked or stopped scope
   keeps the ended session's View (`stack.view_state` "cancel", `stack.mode` "none"; the
   top-level `view_state` is the raw native payload), and
   `result: {}` is only what a freshly booted scope returns. So neither "a View is
   present" nor `result: {}` is the test. If `observing` is `true`, ask the user
   whether to stop it (`stop_view`) before starting a new target.
3. Confirm thermal/dark readiness: the S50 builds darks at startup and they are
   temperature-linked. If the scope was just powered on or just moved indoors→outdoors,
   advise a 10–15 min acclimation before relying on stacked output. If the user plans
   to use the dew heater, note that enabling it after darks were built invalidates them
   — set it BEFORE the session and let darks rebuild, or accept re-enhancement later.
4. Note the mount mode. If Alt-Az (default), warn that field rotation causes rising frame
   rejection through a pass and a spiral border on the stack — expected, not a fault. How
   quickly it bites depends on where the target sits: worst near the zenith, far slower at
   low declination, so a sweet-band target can run a long clean slot while a near-zenith one
   trails within minutes. Do not treat elapsed time as the trigger. EQ mode (wedge + polar
   align) avoids this.
5. **Read the rig profile if present.** If `docs/RIG-PROFILE.md` exists, read it once now
   and treat its contents as observations about *this specific unit and site* — not as
   general Seestar behavior. If it does not exist, proceed normally and run the relevant
   diagnostic when a symptom actually appears.

## Phase 0.5 — Consult the plan (before acquire)
1. If the user asked to "image tonight" (or similar) **without naming a specific target**,
   do not pick one here — defer target choice to the **`observing-planner`** skill. It
   owns the conditions verdict (`assess_conditions`) and the ranked shortlist
   (`plan_targets`). Come back with a chosen target before proceeding to Phase 1.
2. Once a target is chosen (by the user or the planner), call `get_project(target)`. If a
   project exists, state its progress in ONE line so the user knows what tonight adds:
   `M31: 2.5 h of 6 h — adding more tonight.`
   Projects are created automatically the first time a session is logged, but they have **no
   goal** until someone sets one. With no goal, report the accumulated total instead
   (`M31: 2.5 h collected — adding more tonight.`) and, if the user seems to be building a
   deep image, offer once to set a target with `set_project_goal`. A goal is what makes
   `recommend_projects` and "needs more data" ranking meaningful.
   If no project exists, proceed silently — no need to announce the absence.

## Phase 1 — Acquire target
1. Resolve the target to RA/Dec if the user gave a name (use catalog coordinates; if
   uncertain, say so and ask). Pass **J2000 coordinates in DEGREES** — `goto_target`
   converts RA to the firmware's hours internally. Do NOT pre-convert to hours.
2. `goto_target(name, ra, dec, use_lp_filter)` — set the LP/dual-band filter ON for
   emission nebulae and planetaries (they emit Hα/OIII, which the filter passes and which
   tolerates light pollution and even twilight); leave it OFF for galaxies, clusters,
   reflection nebulae, and broadband targets. State which choice you made and why in one
   line.
3. **Expect a multi-stage acquisition, and budget for it.** A goto normally progresses
   `Initialise` → `3PPA` (a 3-point plate-solve alignment, which runs its own `AutoFocus`)
   → `AutoGoto` → `Stack`. The **first goto of the night** runs that whole sequence,
   autofocus included, and takes **~2–4 minutes** before the first frame stacks. **Later
   gotos** usually skip the full `Initialise`/`3PPA` alignment and often stack within
   **~1–2 minutes** (live test 2026-09-24). Budget it into every slot: a 45-minute slot
   yields roughly 41–44 minutes of integration, and a six-target night spends roughly
   7–14 minutes acquiring.
4. **Verify the slew actually happened — `goto_target` returns ok even when the mount did
   NOT move.** Poll `get_view_state`. Healthy progress = plate-solves reaching `complete`,
   the `3PPA` percentage climbing, and `ScopeGoto` `dist_deg` shrinking toward ~0. Two
   failure signatures to catch and act on:
   - **Genuinely stuck:** >~4 min elapsed **and** no solve progress (or repeated solve
     failures) **and** zero frames stacked. That is an unsolvable field — usually
     **obstructed** (roof, tree, wall; typically a low target). Skip it and take the next
     target from the plan; do not wait it out. **Do not diagnose this from elapsed time
     alone** — a normal alignment also sits in `Initialise` for minutes.
   - **Dropped to `ContinuousExposure` with pointing unchanged from before the goto** → the
     mount never slewed (parked, or bad coordinates). Recover via anomaly-playbook; do NOT
     start stacking on a phantom goto.
5. Once stacking has begun, confirm the solve. Prefer `get_view_state` →
   `stack.annotate_state` (`"complete"` once the live stack has solved and annotated the
   field): one device read. `plate_solve` also works, but it polls the device for up to
   ~30 s (`start_solve` plus up to 16 `get_solve_result` reads) and can take that long to
   return.
   If the solve fails, do not rely on the stack — hand off to the anomaly-playbook skill
   (pointing/transparency branch).

## Phase 2 — Focus (usually already done for you)
1. **Acquisition normally focuses the scope.** The `Initialise` sequence a goto triggers
   runs its own autofocus (visible as an `AutoFocus` event reaching `complete`, then the
   focuser settling). In the normal case there is nothing to do here: confirm focus was
   established and record the position from `get_focuser_position` as the session baseline
   for drift detection. A later goto that skips `Initialise` (Phase 1) may not refocus:
   the first acquisition's focus carries over, so do not wait for an `AutoFocus` event
   that is not coming.
2. **`run_autofocus` is optional and firmware-dependent.** The MCP tool exists, but on some
   firmware the underlying device method is unavailable and the call returns `ok: false`
   with `"method not found (code 103)"`. Use it only for a *deliberate mid-session
   refocus* (see the focus-drift branch in anomaly-playbook). **Never block a session on
   it** — if it returns `ok: false`, you already have the focus established during
   acquisition.
3. If the focuser position is implausible, or stars look soft in the Phase 4 framing check,
   hand off to the anomaly-playbook skill (focus branch) rather than improvising.

## Phase 3 — Stack
1. `start_stack`. Confirm via `get_view_state` that the stacking count begins
   incrementing within ~one sub-exposure interval.
2. Record session start time, target, filter, focus baseline, and mount mode to the
   session manifest (the provenance layer logs commands automatically; this is the
   human-facing summary).

## Phase 4 — Monitor (the core loop)
Poll `qa_tier1` on a cadence — every 60–120 s is reasonable; tighten to ~30 s in the
first few minutes and after any focus event. On each poll, report a compact status line:

  `[mm:ss] stacked N (+k) | rejected R | solve OK | focus Δ=±x`

Watch these signals and route faults to the anomaly-playbook skill rather than
diagnosing inline:
- Stacking count flat across two polls → clouds / tracking loss.
- Rejected count climbing fast → check the *signature* before blaming rotation: eccentricity
  rising with FWHM stable = trailing (rotation or tracking; worst near the zenith), while
  FWHM rising with round stars = dew or focus drift, which is correctable at any point in the
  session. Route to anomaly-playbook.
- Focus drifting from baseline → temperature change; consider a mid-session refocus.
- Plate-solve dropping out → pointing / transparency.
- `get_status.mount_tracking` `false` while stacking (the heartbeat's once-per-target
  check, Phase 0) → anomaly-playbook "`mount_tracking` false while stacking". Not yet
  verified mid-stack on fw 8.46; that branch re-slews only when the stack is also flat.
- Drops holding above ~40% past the first ~5 min while the solve stays on target, with the
  LP filter in (`get_view_state.stack.frame_errcode` often 530, usually a bright moon up)
  → anomaly-playbook "sustained drops with the LP filter".

Between agent turns, the slot watcher (Phase 0) is the event source for these signals:
its `STALL`, `DROPS` and `ENDED` lines say which one to check.

**Tier-2 is a POST-session activity, not part of this loop.** `qa_tier2` scores FITS files in
the **local** data directory, so it needs `download_subs` to have run first — and pulling
files off the scope mid-session is exactly what starves the control link (see Operating
notes). So during a session you have Tier-1 telemetry only: treat it as a **health** signal
that tells you whether to keep going, intervene, or stop. Real per-sub FWHM/eccentricity/SNR
arrive at wind-down (Phase 5), where `qa_tier2` / `qa_session_report` run against the
downloaded subs and the **qa-policy** skill interprets them.

If you genuinely need numbers mid-session (e.g. deciding whether to abandon a target), that
requires an explicit pause: stop the stack, download a sample, score it, then resume — say so
and let the user decide, because it costs imaging time and briefly loads the link. Do not do
it silently mid-slot.

### Live reactivity (runs alongside the `qa_tier1` loop)
Alongside the fast Tier-1 polling, keep two slow watches. Keep every message here
compact and phone-friendly — one line, lead with state.
- **Conditions watch (slow cadence, ~every 10 min):** poll `assess_conditions`. If `go`
  flips to False across **two consecutive** slow polls, route to the **`anomaly-playbook`**
  skill (incoming clouds / weather no-go branch) — do not act on a single flip, and do not
  diagnose weather inline.
- **Guardrail watch (unattended runs, same ~10 min cadence):** call
  `check_night_guardrails`. A target slot can run 45+ minutes, and precipitation, dew, or a
  falling battery do not wait for a slot boundary — checking only between targets leaves the
  whole slot unguarded. A `park_and_stop` verdict wins immediately: end the slot and wind
  down (see `autonomous-night` Phase C). So does a check that fails (`ok: false`) — the
  guardrail fails closed (`autonomous-night` Phase B step 1).
- **Keep the tool link warm (~every 5 min while running background work).** An MCP
  connection that goes quiet gets dropped: a client polling continuously survived 8.8 h,
  while sessions with 40–60 min silences died repeatedly mid-run. If you are watching a long
  slot with something other than MCP tools (the slot watcher, for one), call a
  **local-only** tool on a slow cadence — `get_site_profile`, `list_projects` or
  `get_run_state`. All three read local files and
  **cost the device nothing**. Skipping this is why a target boundary arrives with no working
  tool link and the slew has to be improvised.
- **Sweet-band watch:** track where the current target sits in its window, from
  `get_target_observability(target).now` on each heartbeat (Phase 0). When it leaves
  its sweet band — crossing the field-rotation ceiling on the way down, or dropping toward
  the altitude floor / into the horizon mask — tell the user in one line and offer the next
  target from the plan (`plan_targets`), e.g.
  `M27 past its sweet band (nearing floor). Next up: M31 (score 78). Slew?`
  A slew to a new target is a motion command — ask first (see Hard rules / anomaly-playbook).

### Visual framing check (do NOT trust telemetry alone)
`stacked N` confirms frames are landing — NOT that the object is framed, focused, or
cloud-free. **Check the actual field at least once per target, EARLY (~5–10 min in), not
only at the end.** The cheapest source is the live plate-solve annotation: `get_view_state`
→ `stack.target_px` (`[x, y]`) and `stack.target_radius_px` give the object's annotated
centre and radius in the 1080×1920 frame (the `Stack.Annotate` entry whose name matches the
target; `null` when none matches). Alternatively pull the newest sub JPG the scope writes to
its share (on the tested firmware, `_LP_` in the filename confirms the dual-band filter engaged and
`_IRCUT_` means broadband — check your own filenames if the convention differs).

**Filter indices — never guess one.** `set_filter(position)` takes a bare integer, and
**index 0 is `dark`, which closes the shutter**: pick it by mistake and the whole target
images black with no error. Hardware-verified on firmware 8.46:

| index | name | use |
|---|---|---|
| 0 | `dark` | shutter closed — dark frames only |
| 1 | `IRCUT` | broadband |
| 2 | `LP` | light-pollution / dual-band |

The scope publishes its own mapping via the native `get_wheel_setting`
(`{"names": ["dark", "IRCUT", "LP"]}`); read that rather than trusting this table if a
firmware reorders the wheel, and read back `get_wheel_position` after any change.
Confirm three things: the object is **in frame**, stars are **tight** (focus good), and the
background is **clean** (no cloud haze).

**If the object is off-centre, classify the offset before reacting.** The frame centre is
(540, 960); compare it against the annotated centre (`stack.target_px`).

**Never judge framing from solve coordinates.** `plate_solve`'s `ra_deg`/`dec_deg` (like
`stack.solve_ra_deg`/`solve_dec_deg`) and the FITS header `RA`/`DEC` report a position near
the COMMANDED target, not the true field centre. On M1 the solve sat ~4′ from the target
while the object sat ~23′ off-centre in the frame (live test 2026-09-24). Only the Annotate
pixel position measures framing.

| Evidence | Reading | Action |
|---|---|---|
| Offset **varies** between runs, or appeared after a bump, move, or travel | Alignment/level problem — **fixable** | Re-level, re-run a dark-sky alignment, and verify the site/time the scope is using |
| Offset **persists across different sky angles** AND survives a power-cycle + re-level + fresh dark alignment | **Systematic** to that unit | Accept it while the object is fully captured; record it in the rig profile and stop re-testing it |
| Object **cut off at a frame edge** | Framing failure, whatever the cause | Re-acquire; if it recurs at that sky angle, compose around it |

Two captures at **different sky angles** are the minimum evidence for "systematic" — a
single off-centre frame proves nothing, because alt-az rotation smears a fixed angular error
around the frame as the target moves. Once classified as systematic, do not spend session
time or power-cycles chasing a re-centre; note it and keep imaging. (For calibration: one
reference S50 measured a ~20–30′ frame-left offset that persisted through a power-cycle,
re-level, and fresh dark alignment. Confirmed against image data on 2026-09-24: averaged raw
subs placed M1 where Annotate said it was.)

For faint nebulae a single 10 s sub barely shows the object — that is normal; the
accumulated stack reveals it. The check here is framing/focus/clouds, not depth.

Only interrupt the user proactively for: a fault the anomaly-playbook says needs a
decision, a quality collapse, a target leaving its sweet band, a framing/obstruction
problem, or a requested milestone (e.g. "ping me at 1 hour integration"). Otherwise let
the session run quietly.

## Phase 5 — Wind down
1. `stop_view("Stack")` to end stacking cleanly.
2. `download_subs(target=<target>, dest=<local dir>)` to pull the subs to the local data
   dir. Pass arguments **by keyword** — the second positional parameter is `names`, not
   `dest`. **There is no server-side "since" filter:** this pulls what the scope holds for
   that target, so if earlier nights' subs for the same target are still on the device they
   come too — and mixing nights corrupts session-relative QA thresholds. When the device
   still holds prior data for the target, call `list_subs(target)` first and pass just
   tonight's filenames: `download_subs(target=<target>, names=[...], dest=<local dir>)`.
3. Run `qa_session_report(target)` to produce the JSON+Markdown report and the keep-list.
   Summarize for the user: total integration, kept vs rejected counts, median FWHM,
   the dominant rejection cause if any, and where the report and keep-list were written.
4. **Log the session to the project.** After `qa_session_report`, call
   `log_session_result(target, integration_minutes, subs_total, subs_kept, median_fwhm?)`
   so the project's integration accumulates across nights:
   - `integration_minutes` = kept subs × exposure_s ÷ 60 (e.g. 150 × 10 s ÷ 60 = 25 min);
   - `subs_total` / `subs_kept` come straight from the report's counts;
   - `median_fwhm` is the report's median FWHM if present (omit if not).
   Then state the updated project progress in one line:
   `M31 logged: +25 min → 3.0 h of 6 h.`
5. If the user is done for the night, `park` the mount and confirm the fold:
   `get_status.mount_parked` is `true`. The fold typically completes in ~20–30 s (live
   test 2026-09-24); keep polling up to ~4 min before calling it failed.
   Not `tracking`, which is Alpaca's view and disagrees with the device on this hardware.
   `shutdown` only if they ask — shutdown ends the seestar_alp link.

## Hard rules
- Never start stacking on a failed plate-solve.
- Never claim data is "good" without Tier-2 numbers; Tier-1 telemetry is a health
  signal, not a quality verdict. Those numbers only exist **after** the subs are downloaded
  at wind-down — so during a session, report health and say the quality verdict is pending;
  don't imply you know it yet.
- Treat Alt-Az rotation trailing — eccentricity rising while FWHM holds, worst near the
  zenith — as expected; do not raise it as a fault. Never dismiss *rising FWHM with round
  stars* that way: that is dew or focus, and it is correctable.
- Confirm each motion command's success before issuing the next — `ok: true`, then its
  concrete signal (top of this run-book). A park is confirmed only by
  `get_status.mount_parked == true`.
- At wind-down, always log the session to the project (`log_session_result`) so
  integration accumulates toward the goal across nights.

## Operating notes
- **Three tiers of traffic, not two.** What competes with the control link is anything on the
  scope's radio — which is *not* the same as anything that goes through a tool:

  | Tier | Examples | Policy |
  |---|---|---|
  | **Local-only** | `get_site_profile`, `list_projects`, `get_project`, `get_run_state`, `qa_tier2` on downloaded files | poll freely — reads local files, costs the device nothing |
  | **Device-touching** | `get_status`, `get_view_state`, `get_focuser_position`, `check_night_guardrails` | back off while stacking; a slow liveness check is enough when idle |
  | **Share-touching** | reading the scope's image share directly — thumbnails, previews, offloads | slowest tier; never a full FITS mid-session, never while idle |

  The third tier is easy to miss because it generates **no tool call and no provenance
  record** — it is invisible in the audit log while being the traffic most likely to starve
  the link. `get_status` is also worth knowing about: it fans out to six separate device
  reads (five Alpaca properties plus one native `get_device_state` for the mount state),
  so it is the most expensive cheap-looking call in the set.
- **Do NOT run a heavy file transfer off the scope's share during a session.** Pulling images
  off the scope competes with its control link and can starve it — symptoms range from a
  stalled session to the bridge failing to authenticate. Offload before the session or after
  wind-down.
  (The scope may also drop its share when it sleeps after `park`, so finish offloads while it
  is awake.)
- **A target that drops EVERYTHING (0 stacked, drops climbing) is not automatically clouds.**
  It is usually a **local obstruction** (a low bearing over a roofline or tree line is the
  usual offender) or a **drifting cloud bank**, not a global condition. Disambiguate: slew to
  a high target in a **different** part of the sky. If it stacks clean → the first spot was
  locally blocked, skip it and image elsewhere. If it also drops → it is global (dew /
  widespread cloud) → wind down. Don't end the night on one bad patch of sky. A recurring
  blocked bearing is what the learned horizon mask is for.
