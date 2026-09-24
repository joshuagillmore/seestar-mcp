# CLAUDE.md — working in seestar-mcp

Guidance for Claude Code (and humans) working on this repo. Read this before making changes.

## What this is

An **auditable MCP server + Claude Code Skills** to control a ZWO Seestar S50 smart telescope
(via `seestar_alp`'s ASCOM Alpaca API) and run two-tier data-quality QA on its FITS output,
plus a full **observing planner** (conditions verdict, ranked targets, projects/history,
learned horizon mask, autonomous night). Built for high-assurance / air-gapped-adjacent use:
provenance-logged, hash-pinned deps, least-privilege, human-in-the-loop for anything
destructive. Runs on a Jetson, driven from the Claude phone app via Remote Control.

## Toolchain — `uv` only

There is **no bare `python`** on the dev machine (Windows Store shim). Always:

```bash
uv run pytest            # full suite (534 tests, all green)
uv run ruff check src tests
uv run python -m seestar_mcp.server   # launch the MCP server (stdio)
uv run python -c "..."   # one-off checks
```

No new dependencies without strong justification — the astronomy is `astropy` (already
pinned), weather is `httpx`, FITS QA is `photutils`/`numpy`. Deps are exact-pinned and
hash-locked in `uv.lock`; re-run `uv lock` if you touch `pyproject.toml`.

Refinement (stacking/preview) lives in a separate repo, `seestar-refine` (split out
2026-08-08; not yet published). Nothing here imports it, and it imports nothing here. `qa_tier2` is deliberately in both — see that repo's
`docs/QA-PARITY.md`; a threshold change must land in both.

## Architecture — MCP = access, Skills = procedure

- **MCP tools** (`src/seestar_mcp/server.py`) = access + reproducible computation. 34
  single-purpose tools on one `SeestarController`. Each returns a JSON dict with `"ok"`,
  catches errors → `{"ok": false, "error": ...}`, and is provenance-logged. Motion/destructive
  tools have honest `SIDE EFFECT` descriptions.
- **Skills** (`skills/*/SKILL.md`) = judgment. `run-session`, `qa-policy`, `anomaly-playbook`,
  `observing-planner`, `autonomous-night`. Skills decide *whether/what*; tools *do*.
- **Modules:**
  - `alpaca_client.py` — async ASCOM Alpaca client + the `method_sync` action tunnel.
  - `data_client.py` — list/download FITS subs (HTTP + SMB fallback), path-traversal-hardened.
  - `qa_tier1.py` — firmware telemetry health signals (never a quality verdict).
  - `qa_tier2.py` — photutils per-sub grading (FWHM/HFR/ecc/SNR/background/star-count/wFWHM/
    `scattered_light`) → PASS/MARGINAL/REJECT, session-relative, reason-tagged.
  - `planning/` — `astro.py` (deterministic ephemeris + field-rotation sweet-band), `catalog.py`
    (120 DSOs), `site.py`, `weather.py` (meteoblue when `SEESTAR_METEOBLUE_API_KEY` is
    set, else keyless Open-Meteo — see `resolve_source`), `lightpollution.py`, `ranker.py`,
    `projects.py`, `obstructions.py` (learned mask), `autonomous.py` (guardrails + scheduler).
  - `run_state.py` — persisted tri-valued run state, so a restarted server can answer
    "is a run underway, and what is it doing?" without inference.
  - `native_reply.py` — pure parsers for native replies (`_native_error`, `_summarize_view_state`),
    shared by `server.py` and `slot_watch.py` so both read `observing` identically.
  - `slot_watch.py` — the shipped slot watcher (`python -m seestar_mcp.slot_watch`): polls only
    `get_view_state` and prints one line per stacking event for a Claude Code Monitor.
  - `config.py` (pydantic-settings, `SEESTAR_` env prefix), `provenance.py`, `secrets.py`.

## Non-negotiable conventions

- **Determinism:** nothing in `planning/` reads the clock. Astronomy/scoring take an injected
  `now_utc`/`when_utc`; only the *tool layer* resolves "tonight" via `datetime.now(timezone.utc)`.
  This keeps tests stable — preserve it.
- **Never-raise on tool paths.** `analyze_sub`, guardrail/observability cores, and all
  controller methods degrade to error-tagged results, never exceptions.
- **No secrets in config/source/logs.** `provenance.py` redacts; `secrets.py` loads on demand.
- **Reason-tagged verdicts.** Every QA verdict, conditions call, target score, guardrail stop,
  and obstruction suggestion carries human-readable `reasons` — "no unexplained rejects."
- **If a tool names a quantity in prose, return it as a field too.** `reasons[]` *explains* a
  value; it is not a substitute for carrying it. A number that was computed, scored on, or used
  to gate a decision must reach the caller as structured data — otherwise consumers parse
  English or re-derive it, and re-derivation silently drifts from the model. (Adopted 2026-07-31
  after the SeeStar Console review found ten instances: precipitation gated `go` but only
  appeared inside a sentence; the LP classification set 20% of a target's rank and was
  discarded; per-sub QA metrics were computed and dropped at the boundary.)
- **Backward compatibility:** additive optional params (e.g. `rank_targets(projects=None)`,
  `SubMetrics.scattered_light=None`) must reproduce prior behavior exactly — keep the regression
  tests that pin this.
- **Human-in-the-loop for irreversible things:** motion/destructive tools and mask edits
  (`add_horizon_mask`) are only invoked after explicit user confirmation, per the skills.

## Gotchas

- **Alpaca device number is `1`, not 0** (`SEESTAR_ALPACA_DEVICE_NUM=1`). seestar_alp registers
  the scope at the number in its `config.toml` (shipped example = 1). Verified on hardware.
- **Firmware 7.18+ needs the RSA interop key.** The scope silently ignores unauthenticated
  commands. `seestar_alp` handles the handshake if its `interop_pem` points at a
  `seestar_client_key.pem` the user supplies (extracted from the ZWO APK under §1201(f)). This
  server talks *through* seestar_alp, so it never handles the key itself.
- **`# FIRMWARE-DEPENDENT` — status after the fw 8.46 verification (2026-08-03):**
  - `_parse_gps` → `result.location_lon_lat` as `[lon, lat]` — **VALIDATED** on 8.46.
  - `_parse_device_health` → `result.device.is_verified`, and `_parse_battery` →
    `result.pi_status.battery_capacity` — **VALIDATED** on 8.46.
  - `mount.close` (`True` = arm folded) — **VALIDATED**; this is the authoritative park signal.
  - `get_img_file_list` in `data_client.py` — **ABSENT on 8.46.** That name and nine other
    spellings all return `method not found` (code 103). There is probably no native listing
    RPC; harmless because `list_subs` prefers the SMB/filesystem path when `image_root` is set.
  - `get_view_state` — confirmed on fw 7.75 and still valid on 8.46:
    `result.View.Stack.stacked_frame` / `dropped_frame`. `result: {}` appears only on a
    fresh boot — a PARKED scope keeps the ended session's `View`, with `state: "cancel"`
    and `mode: "none"` (live test 2026-09-24), so "observing" means `View.state ==
    "working"` AND `View.mode != "none"`, not "`result` is non-empty".
- **Filter wheel indices (fw 8.46, hardware-verified):** `0 = dark`, `1 = IRCUT`, `2 = LP`. The
  device reports its own mapping via `get_wheel_setting`; read the current index with
  `get_wheel_position` and busy/idle with `get_wheel_state`. Prefer reading the mapping over
  trusting the constants if a firmware reorders the wheel.
- **Alpaca disagrees with the device on two fields — trust the native `get_device_state`.**
  Alpaca `/atpark` and `/tracking` reported `false`/`true` while the device reported
  `mount.close: true` (folded) and `mount.tracking: false`. Reproduced on both 7.75 and 8.46.
  Anything deciding whether the scope is parked or tracking must read the native state.
- **Firmware replies `{"error": ..., "code": 0}` from `pi_output_set2` although the change
  applies — code 0 is success (live test 2026-09-24).**
- **`goto_target` takes J2000; the firmware expects JNow — the server precesses (live test
  2026-09-24: 9–22′ offsets matched precession).** `planning/astro.py` `j2000_to_jnow` /
  `jnow_to_j2000` use FK5 at the equinox of date: precession only, no IERS download. Callers
  must not pre-precess, or it is applied twice. Solve coordinates (`plate_solve.ra_deg`/
  `dec_deg`, `get_view_state.stack.solve_ra_deg`/`solve_dec_deg`) ARE the field centre, in
  JNow; compare the catalog with the `*_j2000_deg` fields beside them, never the JNow pair.
- **Line endings:** commit with `git -c core.autocrlf=false commit`. Commit diff stats can look
  inflated (CRLF↔LF renormalization) — the content diff is what matters; tests are the gate.
- **Field rotation (alt-az):** rank on *sweet-band* time `[min_alt, ~60° ceiling]`, NOT raw
  altitude — rotation is worst near the zenith. See `astro.py` / `ranker.py`.

## Testing

TDD, subagent-driven. All tests run offline: hardware (alpaca/device state), weather
(Open-Meteo via `respx`), and GPS are mocked; astronomy uses fixed timestamps; FITS QA uses
seeded fixtures (`tests/fixtures/*.fits`, incl. `good`/`bad_ecc`/`bad_snr`/`hazy`). No test
touches the network or real hardware.

## Branching & done

Branch for any non-trivial change (never commit to `main`); keep each branch one
coherent concern; merge promptly and delete. "Done" = `uv run pytest` green **and**
`uv run ruff check src tests` clean — never claim done without them.

## Deploy / run

- MCP transport is **stdio** — no inbound socket. Register BEFORE starting a Remote Control
  session (can't add MCP mid-session): `claude mcp add seestar-mcp -- uv --directory <repo> run
  python -m seestar_mcp.server`.
- Startup order and the hardened Jetson systemd unit (`deploy/seestar-mcp.service`) are in
  `README.md` / `SECURITY.md`.

## Where the design lives

Every feature has a spec + plan under `docs/superpowers/specs/` and `docs/superpowers/plans/`
(observing planner P1-P3, learned horizon mask, scattered-light metric). Read the relevant
spec before changing a subsystem.
