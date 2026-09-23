---
name: observing-planner
description: >
  Pre-session planner for a Seestar S50 night: is it worth imaging, and what should
  you shoot? Use whenever the user wants to plan an observing night — e.g. "plan
  tonight", "what should I image tonight", "is it clear enough to image", "build a
  target list", "pick targets for tonight", "give me a shortlist for the Seestar".
  Produces a one-line go/no-go conditions verdict and a ranked, reasoned top-3 target
  shortlist (best window, recommended integration, and the "why"). Consults the
  planning tools for every number — never invents ephemeris or weather — and hands
  the chosen target to the run-session skill for execution.
---

# Seestar S50 Observing Planner

This skill turns "should I image tonight, and what?" into an auditable plan. It runs
in three phases: conditions verdict → ranked shortlist → hand-off. Every number comes
from the planning tools; you interpret and sequence them. Keep output tight and
phone-friendly (Remote Control): lead with the verdict, then the top 3.

## Operating assumptions
- The user is usually reading this on the Claude phone app on a small screen. Lead
  with state, not prose. One-line verdict, then a compact shortlist.
- All ephemeris/weather is deterministic tool output. NEVER guess altitudes, transit
  times, cloud cover, or the moon — always call the tool and quote what it returns.
- The Seestar is alt-az: field rotation, not tracking, sets the clean-integration
  limit. The tools already model this (the sweet band + a near-zenith caveat).
- **Which night.** `assess_conditions`, `plan_targets`, `get_target_observability` and
  `simulate_night` share one `date` rule: omitted means tonight — the coming evening
  from a morning or afternoon call, the current night once it is dark — and a bare
  site-local date (`YYYY-MM-DD`) means the night *beginning* that date's evening. Pass
  the site's calendar date, not today's UTC date: after 00:00Z in the Americas the UTC
  date is already tomorrow, which plans tomorrow night. Each returns
  `dark_window_utc`; quote it when the date could be ambiguous. Everything is relative
  to that night, including a project's `imaged Nd ago`.

## Phase 0 — Conditions (go/no-go)
1. Ensure a **site profile** exists: `get_site_profile`. If it returns none, either
   ask the user to `set_site_profile` (name, lat, lon, and Bortle if known — a Bortle
   sharpens ranking a lot), or note that the tools will fall back to the scope's GPS
   with a default Bortle and that a real profile ranks better. Do not fabricate a
   location.
2. `assess_conditions` (defaults to tonight). State the **verdict in ONE line** with
   the single driving reason, e.g.:
   `GO — clear (5% cloud), 6.2 h dark, 10%-lit moon well separated.`
   `NO-GO — 80% cloud through the dark window; precip 60% after 02:00 UTC.`
3. Read the verdict honestly:
   - `go: true` → proceed to Phase 1.
   - `go: false` → say **why** in that one line (clouds / precip / no dark window /
     bright close moon). Do not pretend the sky is fine. Offer to plan anyway for a
     partial or clearing window ("want a shortlist for after 01:00 when it clears?"),
     but make the caveat plain.
   - `go: null` / `source: "unknown"` → weather is unavailable; say so, plan on
     observability alone, and tell the user to eyeball the actual sky before slewing.
4. **Check the `location` block.** `assess_conditions`, `plan_targets`, and
   `simulate_night` each now return a `location` block (`matched`, `distance_km`,
   `site_name`, `mask_applied`, `warning`) — the scope's live GPS reconciled against
   the saved site profile. Read it before trusting the plan:
   - `location.mask_applied` **False** → the scope has moved beyond the site's
     tolerance, so the saved horizon mask / learned obstructions were **NOT** applied.
     **Disclose it in one line**, e.g.
     `Scope ~48 km from 'Backyard' — horizon mask OFF; set a profile for this location.`
     Do **not** treat the saved mask or obstructions as active for this plan (targets
     behind the home mask are not blocked here — the mask belongs to another site).
   - `location.matched` **null** → GPS is unverified; the plan assumes the saved site.
     Note that assumption **once**, e.g. `GPS unverified — assuming site 'Backyard'.`
   - `location.matched` **True** → the mask applies normally; no extra note needed.

## Phase 1 — Target shortlist
1. `plan_targets` (optionally pass `types`, `min_alt`, or `limit` if the user asked
   for galaxies-only, a higher floor, etc.). The tool returns a ranked, reasoned
   `TargetPlan` list — already horizon-mask filtered and with never-up / no-sweet-band
   targets dropped.
2. Present a compact **top 3**. Per target, one block:
   - **score** and target name/id;
   - **best window (UTC)** from the plan;
   - **recommended integration** as `subs × 10s` (quote `recommended_subs`);
   - the **one-line why** — pull it straight from the plan's `reasons` (sweet-band
     minutes, LP fit for the site's Bortle, moon separation, framing).
   Example line:
   `1. M27 (score 82) · 22:40–01:10 UTC · 540×10s · long sweet-band pass, emission target suits Bortle 6, fits FOV.`
3. **Sequence** the suggestions to catch each target near its sweet-band window and to
   minimize slews (group by rough RA/altitude order; earliest-setting first). Say the
   order in one line.
4. **Respect the tools' exclusions.** Never suggest a target the ranker dropped or one
   behind the horizon mask — if it is not in the returned list, it is not on the plan.
5. **Alt-az caveat.** For any target whose plan notes `field rotation above ceiling`
   (near-zenith transit), add a short caveat: expect earlier frame rejection near
   transit; the clean integration is the sweet-band figure, not the whole pass.
6. **Moon / dew.** If the conditions or a plan flag a bright, close moon or high dew
   risk, mention it once per relevant target — it is a data-quality heads-up, not a
   veto.
7. **Latitude caveat.** At high latitude in summer, astronomical dark can be short or
   absent — the tools return the real dark window, so quote it rather than assuming a full
   night. When dark time is scarce, sequence **broadband** targets (galaxies, clusters) into
   the true dark and **emission/dual-band** targets into twilight, which they tolerate far
   better.

## Phase 2 — Hand off
- Once the user picks a target, hand execution to the **`run-session`** skill
  (pre-flight → acquire → focus → stack → monitor → wind down). This skill plans; it
  does not drive motion.
- QA interpretation of the resulting subs stays with the **`qa-policy`** skill; faults
  mid-session go to **`anomaly-playbook`**.

## Phase 3 — Learn obstructions (log solves, suggest mask)
The horizon mask is *learned-and-confirmed*, not hand-declared: cross-night, clear-sky
plate-solve failures at a low, isolated bearing are what trees / roofline / power lines
look like. Feed the learner, and surface — never auto-apply — what it finds.
1. **Log each plate-solve outcome.** After a solve at a target, call
   `log_sky_result(target=<id>, solved=<bool>)`. Weather is read and gated
   automatically — a failure recorded under a no-go sky is tagged weather-excluded and
   never counts as obstruction evidence — so you do not have to pre-filter clouds.
2. **Surface suggestions at wind-down, or when asked "what's blocking my view?".** Call
   `suggest_horizon_mask` (read-only — it suggests, never applies). Present each
   candidate arc **with its evidence** — the failing bearing, the number of distinct
   clear nights, and the failure rate — e.g.
   `Seen 90–100° / ~22° fail on 4 clear nights (5/6 solves) — a fixed obstruction? Add to the mask?`
3. **Confirm before applying.** Offer to add each arc the user approves via
   `add_horizon_mask(az_min, az_max, alt_min)`. Only add the specific arcs the user
   confirms; leave the rest for more evidence.

## Hard rules
- Never invent ephemeris or weather — always call `assess_conditions` /
  `plan_targets` / `get_target_observability` and quote what they return.
- Never suggest a target the ranker excluded or one behind the horizon mask / below
  the altitude floor.
- When `go` is false or unknown, state the conditions caveat plainly; do not dress up
  a bad or unknown sky as good.
- Keep it tight: lead with the one-line verdict and the top 3. Detail on request.
- This skill decides *what and whether*; `run-session` decides *how* and owns all
  motion. Do not issue goto/stack commands from here.
- When `location.mask_applied` is False, disclose it in one line and treat the saved
  mask/obstructions as inactive for that plan — never silently apply a stale mask at a
  moved location.
- Never call `add_horizon_mask` without the user's explicit confirmation of a specific
  suggested arc. `suggest_horizon_mask` only proposes; the mask is only ever edited by a
  user-approved `add_horizon_mask` (or `set_site_profile`).
