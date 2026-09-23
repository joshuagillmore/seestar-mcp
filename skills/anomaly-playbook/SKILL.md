---
name: anomaly-playbook
description: >
  Fault diagnosis and response playbook for Seestar S50 sessions. Use when something
  goes wrong mid-session or a check fails — stacking count stalls, rejection rate
  spikes, plate-solve fails, autofocus fails, focus drifts, the mount loses tracking,
  the connection drops, or QA shows a sudden quality collapse. Maps each symptom to
  likely causes and a concrete response, and says when to act automatically vs when to
  surface a decision to the user. The run-session skill routes faults here.
---

# Seestar S50 Anomaly Playbook

This skill is the decision tree for when a session misbehaves. For each symptom: confirm
it, identify the likely cause, take the safe action, and escalate to the user only when a
judgment call is needed. Always log what you observed and what you did (the provenance
layer captures commands; add a one-line human-readable note).

## Triage order
When multiple symptoms appear at once, diagnose in this order, because earlier items
cause later ones: (1) connection, (2) tracking/mount, (3) plate-solve/pointing,
(4) focus, (5) transparency/clouds, (6) field rotation. Don't treat a downstream symptom
as the root cause.

## Symptom: stacking count flat (no increase across 2+ polls)
Likely causes, in order: clouds rolling in; tracking lost; connection dropped; session
silently stopped.
- Confirm with `get_view_state` and `get_status`. Check star count trend via `qa_tier1`.
- If star count collapsed → clouds. Do NOT touch focus or pointing. Tell the user clouds
  are likely; offer to pause and resume, or keep accumulating (the firmware rejects bad
  frames anyway). User decides whether to wait it out.
- If star count is fine but count is flat → tracking or session issue. Re-check tracking
  state; if tracking stopped, re-issue goto + plate_solve to recover. Surface to user.

## Symptom: rejection rate spiking
Likely causes: field rotation (alt-az); dew on the optics; wind; tracking error.

**Classify by the metric signature, not by how long the session has been running.** Rotation
onset is not a fixed clock — it depends on where the target sits, being worst near the
zenith and far slower at low declination. A well-chosen sweet-band target can run a long
slot with no rotation rejects at all, while a near-zenith one trails within minutes. Never
attribute rejections to rotation on elapsed time alone.

- **Eccentricity/elongation rising, FWHM roughly stable** → field rotation (alt-az) or a
  tracking error. If the target is high or near transit, rotation is the likely cause:
  expected, note it, do not "fix" it — the usable integration is what survives, and EQ mode
  (wedge + polar align) is the structural fix for next time. Check the target against its
  sweet band; moving to a target further from the zenith helps immediately.
- **FWHM rising while stars stay round** → dew on the optics, or focus drift — NOT rotation.
  This is **correctable and can begin at any point in a session**, so never dismiss it as
  "expected". Recommend the dew heater (note it invalidates existing darks — re-enhancement
  needed) and consider a refocus.
- **Isolated eccentricity bursts on a few subs** → a wind gust or momentary tracking error.
  Reject those subs; the session is otherwise fine. For sustained wind, suggest waiting for
  a lull.
- **Star count and SNR falling together** → transparency or cloud, not a mount or optics
  problem. Use the clouds branch above.

## Symptom: incoming clouds / weather no-go
Likely trigger: the run-session conditions watch reports `assess_conditions.go` flipping
False, or cloud cover rising in the forecast, while a session is live.
- **Corroborate before acting.** Do NOT abort on a single cloudy `qa_tier1` poll alone —
  that is one frame's telemetry, not a trend. Confirm with `assess_conditions`: check that
  `go` is False (or cloud clearly rising) and read the driving reason from its output
  (cloud % / precip / dark-window). If the forecast still says `go: true`, treat a brief
  star-count dip as the "stacking count flat" / transparency case above, not a weather abort.
- If confirmed (`go` False / cloud rising): **PAUSE stacking** and alert the user with the
  forecast reason in one line, e.g.
  `Clouds moving in — forecast 80% cloud through 02:00 UTC. Pause and wait, or wind down?`
- Offer the two choices plainly: **wait it out** (leave the session up; the firmware rejects
  bad frames anyway, so accumulation simply stalls) vs **wind down** now.
- On a **hard no-go** — precipitation, or a sustained no-go with no clearing in the dark
  window — recommend winding down: stop the stack, run the Phase 5 wind-down (including
  logging the session), and `park` the mount to get the optics horizontal.
- Pausing, winding down, and parking are all state-changing — **always ask first**; never
  auto-abort a session on weather.

## Symptom: goto seems stuck (but may be a normal alignment)
Likely cause: the normal `Initialise`/`3PPA` alignment the firmware runs on a goto, which
takes minutes and includes its own autofocus — NOT a fault. This is the most common misread.
- Judge by **progress, not elapsed time**: a healthy alignment shows plate-solves reaching
  `complete`, the alignment percentage climbing, and the goto distance shrinking toward ~0.
  Let it finish (~2–4 min).
- Treat it as a real fault only when **all three** hold: >~4 min elapsed, **no** solve
  progress (or repeated solve failures), and zero frames stacked. Then the field is
  unsolvable — usually an obstruction at that bearing. Skip the target, and log it with
  `log_sky_result(target=..., solved=False)` so the obstruction learner sees the evidence.

## Symptom: bridge connects but authentication fails
Likely causes: another client already holds the scope's single control channel (a phone app
is the usual culprit); a heavy file transfer starving the link; a missing or wrong interop
key on firmware 7.18+.
- A **single** failed handshake at startup is not conclusive — the bridge retries on its
  heartbeat and often authenticates seconds later. Watch for the retry before acting.
- If it keeps failing: close the phone app completely, stop any transfer off the scope's
  share, confirm the scope has finished booting, then restart the bridge.
- If it started right after a firmware update, suspect the auth handshake; do not attempt to
  patch it mid-session.

## Symptom: plate-solve fails
Likely causes: bad pointing; thick cloud/poor transparency; focus far off (no stars to
solve).
- Retry `plate_solve` once. If it fails again, check star count: near-zero stars →
  transparency/clouds or gross defocus, not a pointing bug.
- If stars are present but solve fails → re-issue `goto_target` to re-acquire, then solve.
- NEVER start or continue stacking on an unsolved field. If solve can't be recovered,
  surface to the user — likely sky conditions.
- **Feed the obstruction learner.** After corroborating with weather (as above), log the
  outcome with `log_sky_result(target=..., solved=False)` so the (az, alt) obstruction
  histogram learns. It is weather-gated automatically — a failure under a no-go sky is
  recorded weather-excluded and never counts toward an obstruction — so a genuine cloud
  night can't wall off good sky. **Never auto-add a mask here.** A persistent low-altitude
  bearing that keeps failing on *clear* nights is exactly what `suggest_horizon_mask`
  surfaces at wind-down for the user to confirm via `add_horizon_mask` (see
  `observing-planner`); a single unsolved field is just a retry.

## Symptom: autofocus fails or returns implausible position
Likely causes: too few stars (clouds/transparency); the target field is sparse; mechanical.
- **First: focus is normally established during acquisition** (the alignment runs its own
  autofocus), so a failing `run_autofocus` is not automatically a problem — see
  `run-session` Phase 2. On some firmware the underlying device method is unavailable and
  the call simply errors; that is not a focuser fault and must not block the session.
- Retry `run_autofocus` once. If it fails again and star count is low → it's sky/field,
  not the focuser; advise waiting or slewing to a richer nearby field to focus, then
  returning. Surface to the user before improvising a slew.
- If it returns a wildly different focus than the session baseline with good star count →
  treat as suspect; re-run once more, and if still divergent, keep the prior baseline and
  flag for the user.

## Symptom: focus drifting from baseline over the session
Likely cause: temperature change (the dominant cause on the S50). Possibly early dew.
- This is the expected slow rise in FWHM over a night. Recommend a mid-session
  `run_autofocus` and update the baseline. If FWHM keeps climbing right after refocus →
  suspect dew, recommend the dew heater (with the dark-frame caveat).

## Symptom: connection dropped / tool calls failing
Likely causes: WiFi instability (try switching bands — 2.4 GHz usually reaches farther,
while 5 GHz is cleaner where 2.4 GHz is congested);
seestar_alp restarted; firmware auth handshake broke after an update.
- Retry the call once. If the Alpaca layer is unreachable, the issue is seestar_alp or
  the network, not the scope — tell the user to check seestar_alp (:5555) and the
  Seestar's WiFi. If it started right after a firmware/app update, suspect the 7.18+ RSA
  auth handshake broke; the auth/command-map module needs updating (do not attempt to
  patch it mid-session).
- Remote Control note: if the USER's app connection drops, the local session keeps
  running **only if the machine running Claude Code stays awake** (a dedicated always-on host
  near the scope). Confirm that before reassuring anyone: on a laptop that sleeps or
  hibernates, the session dies with it and an unattended run stops mid-night with the mount
  still tracking. Say which case applies rather than giving blanket reassurance.

## Symptom: sudden quality collapse in QA (FWHM/eccentricity jump, SNR/star-count drop)
- Use the qa-policy skill's pattern table to attribute the cause (focus vs clouds vs
  tracking vs background change). Take the matching action above. Reject the affected
  window; keep the good subs. Don't discard the whole session for a transient window.

## When to act automatically vs ask
- **Act automatically (single retry, then report):** plate-solve retry, autofocus retry,
  re-issuing goto to recover tracking, logging field rotation.
- **Always ask first:** any new/unplanned slew to a different field, enabling the dew
  heater (dark-frame impact), pausing/ending the session, parking or shutting down,
  waiting out clouds vs continuing.

## Hard rules
- Diagnose root cause before acting; don't refocus a cloud problem or re-point a focus
  problem.
- Expected phenomena (Alt-Az field rotation, slow thermal focus drift) are noted, not
  "fixed" and not reported as faults.
- One automatic retry maximum, then surface to the user.
- Never continue stacking on an unsolved field.
- Preserve good data: reject the bad window, not the whole session.
