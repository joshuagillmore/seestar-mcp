# Live-test follow-ups (2026-09-24)

**Spec:** the findings from the first live hardware test of `fix/review-2026-09-22` (night of 2026-09-23 to 09-24, firmware 8.46, driven through the MCP tools). Josh approved all nine follow-ups. Each task restates its finding, and the task text is the authority. The firmware replies quoted below were **captured live that night**, so use them verbatim as test fixtures.

**Branch:** `fix/live-test-followups`, taken from `fix/review-2026-09-22` @ 83685b7. Work happens in the worktree `C:\Users\joshu\SeeStar-AI-live-followups`. NEVER edit the main checkout `C:\Users\joshu\SeeStar-AI`: live MCP servers load code from it. Aim for one commit, or a small series, per task.

## Global Constraints

- **Toolchain:** use `uv` only (`uv run pytest`, `uv run ruff check src tests`, `uv run python -c ...`). No bare python. No new dependencies.
- **Done** means `uv run pytest` fully green AND `uv run ruff check src tests` clean. The baseline is 403 passed at 83685b7.
- **TDD:** write the failing test first and watch it fail for the right reason.
- **Committing:**
  - Commit with `git -c core.autocrlf=false commit`, ending the message with the session trailer lines. Check `git show HEAD:<file> | tr -cd '\r' | wc -c` is 0 for every file touched; this Windows machine has introduced CRLF before.
  - Do not push, and do not touch `main` or `fix/review-2026-09-22`. Do not edit `.env*`, `.sops.yaml` or `data/`.
- **Determinism:** nothing in `src/seestar_mcp/planning/` reads the clock. Only the tool layer (`server.py`) uses `datetime.now(timezone.utc)`.
- **Never-raise:** controller methods and MCP tools return `{"ok": false, "error": ...}`.
- **Backward compatibility:** changes are additive only. Never remove or rename an existing output key; `tests/test_console_contract.py` pins them for the SeeStar Console.
- **Quantities as fields:** if a tool names a quantity in prose, it returns it as a field too.
- **Comments:** match the surrounding comment idiom. Comments explain *why* and cite the finding and date, e.g. "live test 2026-09-24".
- **Privacy:** never write the observing site's coordinates, place names, or times/altitudes computed at the real site into code, tests, docs or commit messages; the repo is public. Test sites must be synthetic, e.g. the `DC` sample site already used in `tests/test_planning_astro.py` (lat 38.9, lon -77.0), or Greenwich.

## Task 1: Error replies with code 0 are successes; a real mount fixture

**Finding (live):** the dew heater's native `pi_output_set2` answered every call with
`{"jsonrpc": "2.0", "Timestamp": "663.578618899", "method": "pi_output_set2", "error": "expected object param", "code": 0, "result": 0, "id": 10104}`
while it DID apply the change. This was verified by switching the heater off and on and reading `get_device_state` → `result.setting.heater_enable` flip each time. `_native_error` (`src/seestar_mcp/server.py`, ~line 1560) treats any truthy `"error"` key as a failure, so `set_dew_heater` returns a false `ok: false`. Real failures carry a nonzero code: `"method not found"` is code 103, `"no solve data"` is code 215 and `"fail to operate"` is code 207.

Separately, the mount-state tests use a documented shape rather than a captured one. The live `get_device_state` → `result.mount` block was `{"move_type": "none", "close": false, "tracking": false, "equ_mode": false}` while imaging, and `close` became `true` after park.

**Requirements:**
1. Failing tests first:
   - `_native_error` on the captured `pi_output_set2` reply returns `None` (success).
   - `set_dew_heater` given that reply returns `ok: true` and a `warning` field containing `"expected object param"`.
   - The existing code-103/207/215 error dicts still fail.
   - A dict with a truthy `error` and NO `code` key still fails, because a missing code is not proof of success.
2. `_native_error`: a dict with a truthy `"error"` fails ONLY when `code` is present and nonzero, or absent. `code == 0` (int 0) is success.
3. Surface the firmware's text on success as an additive `warning` field. Put it in `_native_fail`'s callers' success path generically, e.g. a small helper `_native_warning(value) -> str | None` that callers merge into the success dict, rather than editing each method by hand if a generic route exists. Every controller method that uses `_native_fail` should carry `warning` when present. Keep the existing AST guard test green and extend it if needed.
4. Replace or extend the mount-state fixtures in the tests (the `_parse_mount_state` tests and the get_status test) with the captured block above, in both the imaging state (`close: false`) and the parked state (`close: true`). Keep the flat-mock cases too.
5. Update the `_native_error` docstring with the captured reply and the date.

## Task 2: `plate_solve` waits for the solve

**Finding (live):** `plate_solve` calls `get_solve_result` immediately after `start_solve`. On firmware 8.46 the device answers
`{"jsonrpc": "2.0", "Timestamp": "10925.804433318", "method": "get_solve_result", "error": "no solve data", "code": 215, "id": 13769}`
because the solve has not finished, and the tool now returns `ok: false`. Before the review branch it returned a phantom `ok: true`. Polling about 5 s later got a real result: `result` has keys `ra_dec, fov, focal_len, angle, image_id, state, star_number, duration_ms`. Captured values were `ra_dec: [5.572092, 22.068264]` (RA in HOURS, Dec in degrees), `fov: [0.712755, 1.269035]` (degrees) and `angle: 48.994995`.

**Requirements:**
1. Failing tests first, using a fake `method_sync` that answers code 215 N times then a solved result:
   - `plate_solve` polls and returns `ok: true` with the solution.
   - 215 forever up to the timeout returns `ok: false`, with an error naming the timeout and the last code.
   - A non-215 native error from `get_solve_result` (e.g. code 207) fails immediately without further polling.
   - `start_solve` rejected still fails immediately. That was F1 in the previous tranche and must stay.
   - Tests must not really sleep. Inject the sleep or poll interval so tests run instantly, following the existing test style.
2. Poll `get_solve_result` while it answers code 215, at a modest interval (~2 s) up to a timeout (~30 s default). Make both keyword params with defaults on the controller method. The MCP tool may keep no params.
3. Return additive structured fields next to the existing `solve_result`:
   - `ra_deg` (the RA hours × 15)
   - `dec_deg`
   - `angle_deg`
   - `fov_deg` (pair)
   - `star_number`
   - `solve_duration_ms`
   - `waited_s`

   Keep the existing `solve_result` key unchanged.
4. Code 215 is "in progress" ONLY inside this polling loop. Everywhere else it stays an error.
5. Update the tool description: the call may take up to ~30 s, and RA is returned in degrees.

## Task 3: `qa_tier1` stops flagging a false stall after a target change

**Finding (live):** after each target switch, `qa_tier1` reported `flags: ["stacking_stalled"]` and `stacked_delta: -274`. The trend compares the new target's stack count (e.g. 26) with the previous target's final count (300), because `Tier1Monitor` (`src/seestar_mcp/qa_tier1.py`, trends ~lines 380-420) never resets its baseline.

**Requirements:**
1. Failing test first: a monitor that saw stacked=300 and then sees stacked=26 must NOT flag `stacking_stalled`. Its `stacked_delta` must be `None` or reflect the new session, not -274. A later poll within the same stack (26 → 94) must trend normally.
2. Treat a decrease in `stacked` (or, if the snapshot carries it, a change of target) as a new stack. Reset the trend baseline so deltas and stall detection start fresh.
3. Keep every existing `qa_tier1` test green, and add a reason or comment explaining the reset.

## Task 4: `get_target_observability` reports the target's position now

**Finding (live):** every heartbeat needed a scratch astropy script to get the current target altitude and azimuth against the floor and ceiling. The tool reports the whole night, not now.

**Requirements:**
1. Add an ADDITIVE `now` block to `get_target_observability`'s result:

   `{"utc": <ISO now>, "alt_deg": float, "az_deg": float, "above_floor": bool, "in_sweet_band": bool}`

   - The floor is `site.min_altitude_deg`; the sweet band is `[min_altitude_deg, field_rotation_ceiling_deg]`.
   - Compute it with the existing pure `planning.astro.azalt_at(site, target, when_utc)` (it returns `(az, alt)`). The tool layer passes the real clock, keeping `planning/` clock-free.
   - Honour the horizon mask as the rest of the tool does: if the mask blocks that alt/az, `in_sweet_band` and `above_floor` must reflect that. Use the existing `is_blocked` helper.
2. Tests, written first: with a monkeypatched clock (follow how other tool tests pin "now"), the `now` block matches `azalt_at` at that instant on a synthetic site. The booleans flip correctly across the floor and ceiling. The block is present on every `ok: true` path.
3. The block is about the real current time even when `date` is given, which chooses a night. Name and document it so that is unambiguous.

## Task 5: `get_view_state` returns a compact `stack` summary

**Finding (live):** every framing and drop check needed a scratch script to dig `stacked_frame`, `dropped_frame`, `frame_errcode` and the Annotate pixel positions out of the raw `get_view_state` payload.

The live payload shape, while stacking:
- `result.View` has the keys `state, lapse_ms, mode, cam_id, target_ra_dec, target_name, lp_filter, gain, Stack, stage`.
- `result.View.Stack` was `{"state": "working", "lapse_ms": 141126, "frame_errcode": 530, "stacked_frame": 3, "dropped_frame": 7, "can_annotate": true, "PlateSolve": {"state": "complete", "lapse_ms": 2763, "ra_dec": [18.89505, 33.015196]}, "stage": "PlateSolve", "Exposure": {"state": "working", "lapse_ms": 10033, "exp_ms": 10000.0, "port": 4700}}`.
- `Stack.Annotate` was `{"state": "complete", "result": {"image_size": [1080, 1920], "annotations": [{"names": ["NGC 6720", "Ring Nebula", "M 57"], "pixelx": 438.0, "pixely": 672.0, "radius": 16.0}, ...], "image_id": ...}}`. Annotation names can contain non-ASCII characters (e.g. "ν1 Lyr").

A freshly booted scope that has run no view replied `{"jsonrpc": "2.0", "Timestamp": "606.130373434", "method": "get_view_state", "result": {}, "code": 0, "id": 10078}`.

**AMENDED (dashboard read-only pass, 2026-09-24 12:00Z):** a PARKED scope does NOT return `result: {}`. It keeps the ended session's View, with `result.View.state == "cancel"`, `result.View.mode == "none"`, `Stack.state == "cancel"`, the final `stacked_frame` (1003) and `frame_errcode` 266. Observed `View.state` values are `"working"` (active session) and `"cancel"` (ended or stopped); `"complete"` appears on sub-steps. Assume a `"fail"` value exists. So "observing" must mean `View.state == "working"` and `View.mode != "none"`, NOT "result is non-empty".

**Requirements:**
1. Keep `view_state` (raw) unchanged, and ADD these fields:
   - `observing: bool`: true ONLY when `View.state == "working"` and `View.mode != "none"`. It is false for `result: {}`, for an ended or cancelled session, for any other state, and for junk.
   - `stack`: `null` when there is no `View` at all (`result: {}`). It is present whenever a View exists, INCLUDING an ended session, so its final counts stay visible:

     `{"target_name", "view_state", "mode", "stage", "state", "lp_filter", "stacked", "dropped", "frame_errcode", "solve_ra_deg", "solve_dec_deg", "annotate_state", "target_px": [x, y] | null, "target_radius_px" | null}`

   `target_px` is the annotation whose `names` match `target_name`: normalise case and spaces, so "M 57" matches "M57". Use null when there is no match. Values missing from the payload are null. A pure `_summarize_view_state(state) -> tuple[bool, dict | None]` next to the other parsers returns `(observing, stack)`.
2. Tests, written first, use the captured payloads above: stacking (`observing` true), fresh-boot `result: {}` (`observing` false, `stack` null), and a parked/ended session built from the amended facts (`state` "cancel", `mode` "none", `stacked_frame` 1003, `frame_errcode` 266; `observing` false, `stack` present with the counts). Add a partial or odd payload (missing `Stack`, non-dict) that never raises.
3. Update the tool description to mention the summary.

## Task 6: Ship the slot watcher

**Finding (live):** a quiet event stream kept the whole night observable without noise. It was a scratch script run under a Monitor, polling `get_view_state` once a minute and emitting a line only for:
- a stage change;
- a burst of ≥3 drops in one poll;
- a stall (stacked flat for 3 consecutive polls while stage is `Stack`);
- the view becoming idle. AMENDED: this means `observing` turning false, using Task 5's rule. A parked or ended session reads `View.state` "cancel", not `result: {}`. Emit one line saying the session ended, with the final counts and the errcode.
- an error;
- every 60 stacked frames.

It should live in the repo, next to the dawn watchdog, as a supported tool.

**Requirements:**
1. New module `src/seestar_mcp/slot_watch.py`:
   - A pure, testable `SlotWatcher` class with `observe(view_state_reply, now_utc) -> list[str]`. It is stateful across calls and emits the event lines above; reuse Task 5's `_summarize_view_state` if it is in place, which it will be because tasks run in order.
   - A CLI: `uv run python -m seestar_mcp.slot_watch --duration 1740 --every 60 --drop-burst 3 --milestone 60 --stall-polls 3`. It builds the Alpaca client from settings (as `SeestarController.from_settings` does), polls `get_view_state` every `--every` seconds, prints each event as one flushed UTF-8 line prefixed `HH:MM:SSZ`, prints a final "watch window ended" line, and exits 0 at the end.
   - It never raises out of the loop: a failed poll prints an `ERR` line and continues.
   - It touches only `get_view_state` (device-touching tier, 1/min) and never the share or motion.
2. Unit tests for `SlotWatcher.observe` over a scripted sequence of captured-shape payloads, covering each event type, no event on a quiet poll, and target-change handling (a new target resets the milestone and stall counters). Test the CLI argument parsing lightly. No network.
3. Add a short usage note to `deploy/` (e.g. a comment block at the top of the module plus one line in `README.md`'s run section) showing how to run it under a Claude Code Monitor. Grep with `-a`, or set `PYTHONIOENCODING=utf-8`, because annotation names contain non-ASCII characters.

## Task 7: Contract and docs for this round, plus the two night tests

**Findings:**
- Josh approved the pre-merge extras: tighten the CONTRACT v1.2.0 `date` wording, and add two pinning tests. The dashboard session verified the rule "inside the dark window → that night; otherwise → the next night". The existing in-dark test covers only 22:00 local.
- Tasks 1, 2, 4 and 5 add or alter consumer-visible output.

**Requirements:**
1. Tests, written first, at the synthetic `DC` sample site (UTC-4 in September):
   - `planning_when` at `2026-09-23T05:00:00Z` (≈01:00 local, after local midnight, inside dark) returns the instant unchanged, and its `dark_window` is the night that began the previous evening.
   - At `2026-09-23T09:40:00Z` (just after astronomical dawn) it resolves to the NEXT night.
   - Add a tool-level test that an omitted `date` at ≈01:00 local reports the in-progress night's `dark_window_utc`, pinning the clock as other tool tests do.
2. `docs/CONTRACT.md`. v1.2.0 is unreleased (this branch is unmerged), so EXTEND the existing v1.2.0 entry rather than adding v1.3.0:
   - the precise `date` rule: "the night in progress from astronomical dusk to astronomical dawn; the next night after dawn";
   - a `warning` field on command-tool success, from firmware code-0 error replies;
   - `plate_solve`'s additive fields and its polling, which can take up to ~30 s. Caveat: `ra_deg`/`dec_deg` are the solver's REPORTED position, which on fw 8.46 sits near the commanded target and not at the true field centre. Offline image data showed the object ~23′ off-centre while the reported position was ~4′ from it. For framing, use `get_view_state.stack.target_px`.
   - `get_target_observability.now`;
   - `get_view_state.observing` and `.stack`. `observing` is true only when `View.state == "working"`. A parked scope keeps the ended session's View (`state` "cancel", `mode` "none"). `stack` stays present for an ended session and is null only for a fresh `result: {}`.
   - `dark_window_utc` edges carry about ±5 min jitter (5-min sun grid anchored at call time).

   If the contract tests pin keys for these tools, add the new keys to the `_require` lists. The Status row's test count must stay accurate.
3. `CHANGELOG.md`: add entries under Unreleased for Tasks 1–6, in the existing style.
4. `CLAUDE.md`: update the test count to match `uv run pytest --collect-only -q | tail -1` at the time of editing. Add a gotcha bullet: "firmware replies `{"error": ..., "code": 0}` from `pi_output_set2` although the change applies — code 0 is success (live test 2026-09-24)". Also correct the existing `get_view_state` gotcha: `result: {}` appears only on a fresh boot. A parked scope keeps the ended session's View (`state` "cancel", `mode` "none"), so "observing" means `View.state == "working"`.

## Task 8: Skills learn from the live test

**Findings (live, 2026-09-23/24):**
- The arm fold after `park` took ~22 s: `mount_parked` flipped between polls 1.6 s and 18 s after the command. The skills say 1–3 min.
- A goto after the first one of the night skipped the full Initialise/3PPA alignment and stacked within ~1.5–4 min. The first goto of the night ran Initialise with autofocus. The skills say ~2–4 min for every goto.
- M57 with the LP (dual-band) filter and a 93% moon up dropped ~55% of frames. `get_view_state` → `Stack.frame_errcode` was 530, and the plate-solve stayed on target. Re-acquiring the same target broadband dropped 6%. M1 with LP after moonset dropped 0%.
- A repeat `park` on a folded scope and `stop_view` on an idle scope both return code 0 and are harmless.
- `mount_tracking == true` during stacking was NOT verified live, because the heartbeat never called `get_status` mid-stack.
- For M1, a fresh `plate_solve` reported a position ~4′ from the target, while the live-stack Annotate pixel position implied ~23′ off-centre. **SETTLED OFFLINE:** averaging 60 downloaded raw M1 subs puts the nebula at pixel (150, 1372), which matches Annotate's (142, 1395). So the Annotate pixel positions are correct, and the ~20–30′ systematic offset is REAL. The solver's reported `ra_dec` and the FITS header `RA`/`DEC` both sit near the COMMANDED target, not the true field centre. On fw 8.46 they are not a framing measure.
- Tasks 2, 4, 5 and 6 add `plate_solve` fields and polling, `get_target_observability.now`, the `get_view_state` `observing`/`stack` summary, and the shipped slot watcher.
- The dashboard's pass showed that a parked scope keeps the ended session's View (`state` "cancel", `mode` "none"). "Is a session running" must use `get_view_state.observing`, not "View present" and not `result: {}`. Fix any skill text that says `result: {}` means idle (e.g. `run-session` Phase 0.1 step 2). CLAUDE.md's gotcha is Task 7's job.

**Requirements (skills only, surgical edits in each skill's voice; verify every tool, field or flag name against the code at this branch's HEAD):**
1. `run-session` and `autonomous-night`:
   - Fold time is typically ~20–30 s. Keep polling up to ~4 min as the upper bound.
   - The acquisition budget: first goto of the night ~2–4 min, including Initialise/autofocus; later gotos often ~1–2 min.
2. `run-session` Phase 0 and Phase 4:
   - Recommend the shipped slot watcher (`python -m seestar_mcp.slot_watch`) as the Monitor source, in addition to the heartbeat, never instead.
   - The heartbeat should use `get_target_observability(target).now` for altitude vs the floor and ceiling.
   - The heartbeat should call `get_status` at least once per target while stacking, to confirm `mount_tracking` is `true`. If it reads `false` mid-stack, route to the anomaly-playbook tracking branch.
   - Framing checks can use `get_view_state.stack.target_px`.
3. `anomaly-playbook`: add a branch for sustained drops with the LP filter.
   - Symptom: the drop rate stays above ~40% after the first ~5 min of settling while the plate-solve stays on target (`frame_errcode` often 530), typically with a bright moon up.
   - Action: `stop_view` and re-acquire the same target with `use_lp_filter=false`. This is a same-target re-acquire inside an approved plan, so no fresh confirmation is needed in an authorised autonomous night.
   - Cite the M57 numbers without site details.
4. `run-session` framing section:
   - Keep the 20–30′ offset calibration note, now marked as confirmed against image data on 2026-09-24. Averaged raw subs placed M1 where Annotate said.
   - Add one caution: `plate_solve`'s `ra_deg`/`dec_deg` and the FITS header RA/DEC report a position near the COMMANDED target, not the true field centre. Judge framing from the Annotate pixel position (`get_view_state.stack.target_px`), not from the solve coordinates.
   - Keep the "classify before reacting" table.
5. Mention nowhere any site coordinate, place name or time or altitude computed at the real site.
