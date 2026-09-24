# seestar-mcp consumer contract — v1.2.0

The response shapes external consumers may rely on, and the rules for changing
them. Enforced by `tests/test_console_contract.py`, which fails **this** repo's
build rather than a consumer's runtime.

This document replaces the prose exchange that produced it. Diff it; don't read
it end to end.

## Status

| | |
|---|---|
| **Version** | 1.2.0 |
| **Covers** | 11 of 34 tools — the ones a consumer actually parses |
| **Validated against a real consumer** | yes — the SeeStar Console parsed a real 25-sub `qa_tier2` payload with an independently written schema, first try, no changes |
| **Enforced by** | `tests/test_console_contract.py` (21 tests, all 11 pinned at the tool boundary) |

## The rules

1. **Required means present, not truthy.** A required key must exist; its value
   may be `null` unless stated otherwise. Consumers tolerate `null` and do not
   tolerate a key vanishing.
2. **Removing or renaming a required key is a MAJOR change.** So is changing a
   value's unit, sign, or reference frame — those are invisible to a schema and
   are the failures that render a plausible wrong picture.
3. **Adding a key is a MINOR change.** Consumers ignore unknown keys.
4. **Unknown ≠ empty.** A value we do not have is **omitted**, never `[]` or a
   fabricated default. `[]` and "not tracked" render identically and mean
   opposite things.
5. **If a tool names a quantity in prose, it returns it as a field too.**
   `reasons[]` explains a value; it does not replace it. (See `CLAUDE.md` — this
   is a repo convention, not just a contract rule.)

## Covered tools

`get_view_state` · `get_status` · `get_run_state` · `qa_tier1` · `qa_tier2` ·
`list_projects` / `recommend_projects` · `assess_conditions` · `plan_targets` ·
`get_target_observability` · `get_site_profile`

That is **11 tools on 10 lines** — `list_projects` and `recommend_projects` share a
line but are two separate tools, which is what made the count drift twice. The
suite also calls `set_site_profile` and `log_session_result`, but only as fixtures
to create state; their shapes are not pinned.

## Shapes that are load-bearing and easy to break by accident

- **`get_view_state` must stay valid with `result: {}` and no `View` key.** That
  is what a freshly booted scope returns. A parked or ended session does not:
  it keeps its View (`state` "cancel", `mode` "none"). `observing` is the
  running test, not an empty `result`. Fields *on* `View` are deliberately not
  pinned: a mid-acquisition payload carries `Initialise` and `stage` but no
  `Stack` at all.
- **Plate-solve position lives at `Stack.Annotate.result.annotations[]`**, not
  flat on `Annotate`, and `image_size` is a two-element array.
- **`qa_tier2.summary.subs[].name` is the filename STEM** — no extension. A join
  key built as `f"{name}.fit"` matches nothing.
- **Unanalysable subs stay in `subs[]`** with `metrics.error` set. Filtering them
  out shortens a chart instead of reporting a gap.
- **`summary.thresholds` carries the cutoffs actually applied**, session-relative
  ones included — they move night to night, so only these can position a chart's
  cutoff line.
- **`thresholds.eccentricity_marginal` is session-derived as of v1.1.0.** It was
  a fixed 0.42; it is now `max(median + 1.0σ, 0.42)`. No shape change and no
  schema break — but a chart that *hardcoded* 0.42 rather than reading the field
  now draws its line in the wrong place. `eccentricity_reject` is unchanged and
  stays absolute at 0.575. As of v1.1.1 it is guaranteed **finite** and **never
  above `eccentricity_reject`**, so the two lines never cross. If they are equal,
  the MARGINAL band is empty by construction — that is a degenerate
  configuration, not a rendering bug.
- **`qa_tier2.summary.target` echoes the `target` argument**, so it is `null`
  whenever a caller uses `paths=`. Do not build a header on it.
- **`list_projects` / `recommend_projects` default to `detail="summary"`**, which
  **omits** `sessions` rather than emptying it. Pass `detail="full"` for history.
- **`get_run_state.state` is tri-valued** — `active` / `idle` / `unknown`.
  `unknown` means a run was recorded but its stamp is stale; it must never be read
  as "the scope is free".
- **`get_run_state.run` is not a liveness signal.** It is populated in **two**
  states — `active`, and `unknown` when the stamp went stale (the stale record is
  kept deliberately: what was running when we lost track is worth knowing). It is
  `null` only for `idle` and for an unreadable file. Branch on `state`; a non-null
  `run` does **not** mean a session is live.

## Changelog

- **v1.2.0** — three additive changes; no key removed or renamed, no unit or
  frame changed. **`get_status`** gained `mount_parked` and `mount_tracking`,
  each `bool` or `null`, read from the device's native `get_device_state`
  (`result.mount.close` / `result.mount.tracking`). They are the authoritative
  park and tracking signals: the existing `tracking` is Alpaca's view, which
  disagrees with the device on this hardware (fw 7.75 and 8.46), and is left as
  it was. Like the other `get_status` fields, both keys are always present and
  may be `null` (rule 1): `null` means the native read failed — unknown, never
  `false`. That read is one more device call: `get_status` now makes six device
  reads per call (the five Alpaca reads plus one native `get_device_state`), not
  the single `get_device_state` the 2026-07-31 coordination note promised, and
  the native read can wait up to `http_timeout_s` (default 30 s) on a busy
  device; collapsing the five Alpaca reads into that one native read is planned
  follow-up work.
  **`plan_targets`** and **`get_target_observability`** gained a top-level
  `dark_window_utc`, the same naive two-element pair as
  `assess_conditions.dark_window_utc`, naming the night the result describes.
  And **`date`** on `assess_conditions`, `plan_targets` and
  `get_target_observability` is read differently: a bare `YYYY-MM-DD` is now the
  site-local date of the night *beginning* that evening (it parsed to 00:00Z —
  the evening before, in the Americas), and an omitted `date` resolves to the
  night in progress — from astronomical dusk to astronomical dawn; the next
  night after dawn — where a morning or midday call used to return the night
  just ended. Pass the site's calendar date, not the UTC one. An explicit ISO
  instant inside a night is unaffected. `dark_window_utc`'s edges carry about
  ±5 min of jitter: they come off a 5-minute Sun-altitude grid anchored at the
  call's own instant rather than a fixed clock tick, so the same night queried
  a minute apart can shift the reported edges by up to one grid step.
  *Why:* the 2026-09-22 review found no tool exposed the native mount state, so
  nothing — the run-book skills included — could confirm a park, and a morning
  planning call quietly described last night. `dark_window_utc` lets a caller
  check which night it got.

  **Second addition to v1.2.0 (2026-09-24 live test follow-ups), still
  unreleased:** five more additive changes. A native reply whose JSON-RPC
  envelope carries an explicit `code: 0` is now read as success even when its
  `"error"` text is truthy — the dew heater's native `pi_output_set2` answers
  every toggle with `{"error": "expected object param", "code": 0}` while it
  DOES apply the change, confirmed by reading `get_device_state`'s
  `heater_enable` flip. Every command tool (`goto_target`, `start_stack`,
  `stop_view`, `run_autofocus`, `set_filter`, `set_dew_heater`, `park`,
  `shutdown`, `plate_solve`) and the native reads `get_view_state` /
  `get_focuser_position` now carry an additive `warning` key on success: the
  firmware's odd text when there was one, else `None`.
  **`plate_solve`** now polls `get_solve_result` while it answers code 215
  ("no solve data yet"), up to ~30 s (the default `timeout_s`) — this call can
  now take that long to return. Its success payload gains `ra_deg`, `dec_deg`,
  `angle_deg`, `fov_deg`, `star_number`, `solve_duration_ms` and `waited_s`.
  `ra_deg`/`dec_deg` are the solver's REPORTED position, not the field centre:
  on fw 8.46 they sit near the commanded target even when the target is well
  off-centre in the frame — offline image data showed the object ~23′
  off-centre while the reported position was only ~4′ from it. For framing,
  use `get_view_state.stack.target_px`, not these fields.
  A solve still in progress at `timeout_s` fails (`ok: false`) with `error`,
  `raw` (the last `get_solve_result` reply), `waited_s` (seconds spent polling)
  and `last_code` (the last native code seen, which is always 215 on this path).
  **`get_target_observability`** gained a `now` block (`utc`, `alt_deg`,
  `az_deg`, `above_floor`, `in_sweet_band`): the target's position at the REAL
  current instant, independent of `date` — `date` only selects which night
  `dark_window_utc`/`observability` describe.
  **`get_view_state`** gained `observing` (bool) and `stack` (dict or null).
  `observing` is true only when `View.state == "working"` AND `View.mode !=
  "none"` — a parked scope keeps the ended session's View (`state` "cancel",
  `mode` "none"), so a non-empty `result` is not itself "observing". `stack`
  is null when the reply has no View (a freshly booted scope's `result: {}`,
  or an unreadable payload); it stays present — with its final counts — for
  an ended session. Its keys: `target_name`, `view_state`, `mode`, `stage`,
  `state`, `lp_filter`, `stacked`, `dropped`, `frame_errcode`, `solve_ra_deg`,
  `solve_dec_deg`, `annotate_state`, `target_px`, `target_radius_px`.
  `solve_ra_deg`/`solve_dec_deg` carry the same reported-position-not-
  field-centre caveat as `plate_solve.ra_deg`/`dec_deg` above. `target_px`
  (the Annotate pixel position for the current target) is the VERIFIED
  framing measure — it is what the offline image-data comparison above
  confirmed against the true off-centre object, and what `plate_solve`'s own
  caveat points callers to instead of its `ra_deg`/`dec_deg`.
  **`qa_tier1`**'s `snapshot` gained `target_name` (string or `null`), read from
  `View.target_name` (or `Stack.target_name`). The trend baseline, which never
  reset before, now resets when a new stack starts: the target name changed
  (when both polls report one) or `stacked` decreased. So on the first poll of
  a new stack, `trends.stacked_delta`, `trends.rejected_delta` and
  `trends.hfd_delta` are `null` rather than a delta against the previous stack,
  and `stacking_stalled` counts only the current stack's polls.
  *Why:* the live test found every one of these needed a scratch script to dig
  the same numbers out of raw native replies by hand — a false `ok: false` on
  a heater toggle that had actually worked, a `plate_solve` that failed
  outright on a slow solve, a `qa_tier1` poll across a target switch that
  flagged a false `stacking_stalled`, and no field answering "is the scope
  observing right now" or "where is the target really, this instant."
- **v1.1.1** — `thresholds.eccentricity_marginal` is now guaranteed **finite** and
  **never above `thresholds.eccentricity_reject`**. No shape change; both were
  already true for every real session, and re-scoring the 970-sub reference night
  produced byte-identical verdicts, keep-lists and thresholds. Pinned because a
  consumer drawing two cutoff lines may now rely on their ordering.
  *Why:* adversarial review found the derived line could exceed the reject
  cutoff on a session whose median sits near 0.575 — making MARGINAL unreachable
  exactly on the poor night where it matters — and that a non-finite
  `qa_eccentricity_marginal_sigma` produced a `NaN` cutoff, which silently
  disabled the rule (`x >= nan` is always False) and would have made
  `render_json` raise. New setting `qa_eccentricity_marginal_absolute` restores
  an exact cutoff for anyone who wants one.
- **v1.1.0** — `thresholds.eccentricity_marginal` became session-derived
  (`max(median + 1.0σ, 0.42)`), and `qa_tier2.summary.medians` gained
  `eccentricity_sigma`. MINOR, not MAJOR: the key, its unit (eccentricity
  fraction) and its meaning ("the marginal cutoff this session was scored
  against") are unchanged, and this document already told consumers the
  `thresholds` object moves night to night. Flagged prominently anyway, because
  the Console's eccentricity chart is built on this exact field.
  *Why:* measured over 970 real subs, the fixed 0.42 line graded 96.5% of a good
  night MARGINAL — an alt-az S50 baselines near 0.49, so the constant sat at the
  1st percentile of the rig's own output and MARGINAL stopped discriminating.
  Verdict impact, re-scoring the same 970 subs: PASS 25→710, MARGINAL 831→146,
  **REJECT 114→114 and all three keep-lists byte-identical** — the fix moves the
  signal, not the stacking decisions. See
  `docs/superpowers/specs/2026-08-01-eccentricity-marginal-saturation.md`.
- **v1.0.1** — documented `get_run_state.run` nullability (behaviour unchanged;
  the previous announcement said "`run: null` when idle", which is true of `idle`
  and of an unreadable file but wrong for stale-`unknown`, the one case where the
  record is retained *and* untrustworthy).
  *Document corrections after publication, no shape change:* the Status table
  still read `1.0.0` while the title read `1.0.1` — the version row is the field a
  consumer pins against, and a test now asserts the two agree. The coverage count
  said 9 while the list named 10; the list was ahead of the tests, so
  `get_run_state` and `get_target_observability` gained tool-boundary pins rather
  than the claim being softened.
- **v1.0.0** — initial contract, 9 tools.

## Timestamps — two shapes, both deliberate

| Layer | Shape | Fields |
|---|---|---|
| Planning | **naive**, no offset | `dark_window_utc`, `best_window_utc` |
| Projects / provenance / run state | **offset-bearing** | `date_utc`, `ts`, `stamped_utc` |

Both are pinned. A normaliser must handle both; unifying them silently is a
breaking change.

## Units — the invariants a schema cannot see

| Field | Unit | Pinned as |
|---|---|---|
| `cloud_cover_pct` | percent | `0..100`, and `> 1` on a cloudy fixture |
| `moon_illum_frac` | fraction | `0..1` |
| `wind_kph` | km/h | `>= 0` |
| `dark_minutes_*` | minutes | `>= 0`, `<=` the dark window, sweet-band `<=` above-floor |
| `suitability` | 0–100 score | `0..100` |

`type` on a planned target must stay within the eight `TARGET_TYPES` values —
an unknown value is not a parse failure, it is a target that renders unlabelled.

## Changing the contract

A breaking change needs a MAJOR bump **and** a note to consumers before it ships,
not after. The contract tests are the gate: if a change makes one fail, that is
the contract objecting, and deleting the assertion is not the fix. Where a pin is
too strict — it fires on a legitimate payload — loosen the **field-presence**
check and keep the **nesting** and **unit** assertions, which are the ones that
catch silent breakage.
