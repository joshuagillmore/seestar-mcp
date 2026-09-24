# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). `0.x` is pre-1.0 alpha: several
device paths are not yet hardware-validated (see the README "Status & limitations").

## [Unreleased]

### Added
- Open-source release hygiene: `LICENSE` (MIT), `NOTICE` (trademark, §1201(f), and
  third-party attribution), `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, this changelog, an
  `AUTHORS` file, GitHub Actions CI (Linux + Windows), and issue/PR templates.
- Packaging metadata in `pyproject.toml` (SPDX license, authors, keywords, classifiers,
  project URLs).
- `python -m seestar_mcp.slot_watch`: a standalone slot watcher for a Claude Code Monitor.
  It polls only `get_view_state` and prints one line per stacking event (stage/target
  change, a new stack on the same target, a drop burst, a stacked-count milestone, a stall,
  or the session ending) instead of the whole payload every poll. Its native-reply parsing
  (`_native_error`, `_summarize_view_state`, and friends) moved into a new `native_reply.py`
  module shared with `server.py`, so both read `observing` identically and the watcher does
  not pay `server.py`'s FastMCP/astropy/photutils import cost (~5 s vs. ~0.7 s on the dev
  PC) for a process re-armed every 29 minutes.

### Removed
- **Refinement moved to its own repository** (`seestar-refine`, 2026-08-08): the AstroPipe
  pipeline, the `pystack` backend, the `stack_keep_list` / `stretch_master` /
  `check_backends` / `list_masters` / `prepare_pixinsight_handoff` tools, field-rotation
  autocrop, and the `image-refinement` and `astro-processing` skills. This repo now covers
  capture and QA only; `qa_tier2` is carried in both by design. Its `astroalign`,
  `astroscrappy` and `pillow` dependencies went with it.

### Fixed
- Observing planner crashed whenever called with default `date=None` (offset-suffixed
  `datetime.now().isoformat()` rejected by astropy); normalized in `_to_time`.
- GPS parser now matches real firmware 7.75 (`result.location_lon_lat` `[lon, lat]`), so
  plans use the scope's actual location instead of a stale saved site.
- `qa_session_report` raised `AttributeError` (`no attribute 'target'`) on any server that
  had not run `goto_target` in the same process, e.g. right after a restart: the
  per-session state init had become unreachable code inside `_weather_cached`. Restored in
  `SeestarController.__init__`.
- Native JSON-RPC error replies (`{"error": "method not found", "code": 103}`, fw 8.46)
  passed as success, so `park`, `goto_target`, `start_stack`, `stop_view`, `set_filter`,
  `set_dew_heater`, `run_autofocus`, `plate_solve` and `shutdown` returned `ok: true` on a
  command the scope rejected, and `park` cleared the run state although the mount never
  folded. Every command tool (and the native reads `get_view_state` /
  `get_focuser_position`) now returns `ok: false` with the text and code
  (`method not found (code 103)`). `get_status` also carries `mount_parked` /
  `mount_tracking` from the device's native `get_device_state` (`mount.close` /
  `mount.tracking`), the authoritative park and tracking signals; Alpaca's `tracking`
  disagrees with the device on this hardware. That adds one native read, so `get_status`
  makes six device reads per call.
- `assess_conditions`, `plan_targets`, `get_target_observability` and `simulate_night`
  planned last night from a morning or afternoon call, and a bare `date` parsed to 00:00Z
  (the evening before, in the Americas). They now plan the upcoming night (the current one
  once dark), and a bare site-local `YYYY-MM-DD` means the night beginning that evening.
  `dark_window` no longer clips the night at the edge of its ±12 h search grid (dawn came
  back as 08:00Z instead of ≈09:25Z at lat 38.9).
- The planner computes the dark window once per ranking instead of once per catalog
  target — about 2× faster than before these fixes (120 targets: 8.1 s → ≈4.0 s on a
  desktop) — and `plan_targets` / `get_target_observability` now return
  `dark_window_utc`, the night they actually planned.
- `plate_solve` returned `ok: true` with the PREVIOUS solve when the scope rejected
  `start_solve` (`{"error": "fail to operate", "code": 207}`): the start reply was discarded
  and `get_solve_result` handed back the last solution. Any native error from `start_solve`,
  including seestar_alp's timeout string, now returns `ok: false`.
- A native reply's JSON-RPC envelope can carry a truthy `"error"` string alongside an
  explicit `code: 0`: the dew heater's native `pi_output_set2` answered every toggle live
  with `{"error": "expected object param", "code": 0}` while it DID apply the change
  (confirmed via `get_device_state`'s `heater_enable` flipping each time), so `set_dew_heater`
  reported a false `ok: false` on a command that had actually worked. `code: 0` is now read as
  success regardless of the `"error"` text; a missing code is never treated as proof of
  success, so the existing 103/207/215 failures are unaffected. Every command tool
  (`goto_target`, `start_stack`, `stop_view`, `run_autofocus`, `set_filter`, `set_dew_heater`,
  `park`, `shutdown`, `plate_solve`) and the native reads `get_view_state` /
  `get_focuser_position` now carry an additive `warning` key on success: the firmware's odd
  text when there was one, else `None`.
- `plate_solve` called `get_solve_result` immediately after `start_solve` and treated the
  device's `{"error": "no solve data", "code": 215}` — the solve simply not finished yet — as
  a failure. It now polls `get_solve_result` every `poll_interval_s` (default ~2 s, floored at
  0.1 s so `poll_interval_s <= 0` against a device stuck at 215 cannot spin forever) up to
  `timeout_s` (default ~30 s); this call can now take that long to return. On success it also
  returns the additive `ra_deg`, `dec_deg`, `angle_deg`, `fov_deg`, `star_number`,
  `solve_duration_ms` and `waited_s`. `ra_deg`/`dec_deg` are the solver's REPORTED position,
  not the field centre — on fw 8.46 they sit near the commanded target even when the object is
  well off-centre in the frame (offline image data: M1's nebula sat ~23′ off-centre while the
  solved position sat only ~4′ from the catalog position); use `get_view_state.stack.target_px`
  for framing instead.
- `qa_tier1`'s trend baseline never reset. Across each `goto_target` it compared the new
  target's stack count (e.g. 26) directly against the old target's final count (300), so it
  reported `stacked_delta` -274 and flagged a false `stacking_stalled` after every target
  switch. The snapshot now carries `target_name` (`View.target_name`, confirmed on
  fw 7.75; `Stack.target_name` as a fallback), and the monitor treats a decrease in `stacked`,
  or — when the firmware reports a name on both polls — a change of target, as the start of a
  new stack. `stacked_delta`, `rejected_delta`, `hfd_delta` and stall detection now compare
  only within the current stack, so on the first poll of a new stack `trends.stacked_delta`,
  `trends.rejected_delta` and `trends.hfd_delta` are `null`.
- `get_target_observability` reported only the whole night's observability, so checking the
  target's position right now needed a scratch astropy script. It gains a `now` block (`utc`,
  `alt_deg`, `az_deg`, `above_floor`, `in_sweet_band`) at the REAL current instant, independent
  of `date` (which only selects which night the rest of the result describes).
- `get_view_state` exposed only the raw native payload, so every framing/drop check needed to
  dig `stacked_frame` / `dropped_frame` / `frame_errcode` and the Annotate pixel position out
  of it by hand. It gains `observing` (`bool`, true only when `View.state == "working"` and
  `mode != "none"` — a parked scope keeps the ended session's `View`, with `state: "cancel"` /
  `mode: "none"`) and `stack` (a compact summary of target/stage/counts/plate-solve/framing;
  `null` when the reply has no View (a freshly booted scope's `result: {}`, or an unreadable
  payload), and present — with its final counts — for an ended session).

### Changed
- `SECURITY.md`: corrected the tool count (33 + 5), reworded the `seestar_alp` supply-chain
  note (external, operator-installed — not vendored), added a real vulnerability-reporting
  contact, and documented the host-wide SMB insecure-guest caveat for the filesystem backend.
- README: added "Status & limitations", "Prerequisites", and "Legal & trademarks" sections.
- Scrubbed personal data (real LAN IP / username) from tracked source and docs.

### Security
- The meteoblue API key no longer reaches stderr / journald. `FastMCP("seestar-mcp")`
  configures the root logger at INFO with a RichHandler, and httpx logs every request at
  INFO as `HTTP Request: GET <full-url> ...` — the keyed weather fetch
  (`planning/weather.py`) carries the key as the `apikey` query param, so it was landing in
  Claude Code's MCP logs, or journald on the Jetson. `server.py` now raises the
  `httpx`/`httpcore` loggers to WARNING right after `FastMCP(...)`, and a `logging.Filter`
  attached to the `httpx` logger redacts any `apikey=<value>` that still gets through, as
  defense in depth against a future level change reopening the leak. Pinned by
  `tests/test_logging_redaction.py`. **If MCP or journald logs from before this fix have
  left the machine, rotate the meteoblue key.**
- `deploy/seestar-mcp.service` had trailing `# comment` text on seven hardening directives
  (`ProtectSystem`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `ProtectKernelTunables`,
  `ProtectControlGroups`, `MemoryDenyWriteExecute`). systemd only treats `#`/`;` as a comment
  marker at the start of a line; mid-line it becomes part of the directive's value, which
  systemd rejects with only a log warning, silently dropping the directive. Without
  `ProtectSystem=strict` in particular, `ReadWritePaths` confined nothing. Comments moved
  onto their own line above each directive; the unit's header now notes to run
  `systemd-analyze verify` and `systemd-analyze security seestar-mcp` after deploy. Pinned by
  `tests/test_deploy_unit.py`. The other deploy files (`deploy/docker/*`,
  `deploy/dawn_park_watchdog.sh`) were scanned for the same problem and are clean: Dockerfile,
  YAML, TOML and bash comments all behave as their authors intended, unlike systemd's
  ini-like format.
  **Operators: smoke-test service startup after deploying this unit.** The hardening takes
  effect for the first time: the service has never run under these seven directives. With
  `ProtectSystem=strict` + `ProtectHome=yes`, `uv run` (the unit's `ExecStart`) may be
  unable to write its cache or `/opt/seestar-mcp/.venv` and fail at startup. This could not
  be verified without the Jetson. Likely remedies, listed in the unit's header: a
  `UV_CACHE_DIR` under a writable path, adding that path to `ReadWritePaths`, or
  `uv run --frozen --no-sync` against an env pre-synced at deploy time.

## [0.1.0] - 2026-07-05

Initial build (pre-public): auditable `seestar-mcp` FastMCP server (33 tools) driving a ZWO
Seestar S50 via `seestar_alp`'s ASCOM Alpaca API; two-tier FITS QA; observing planner with
projects/history and a learned horizon mask; autonomous-night mode with hard guardrails; a
separate `seestar-refine` service (5 tools) for DeepSkyStacker / PixInsight stacking; and six
Claude Code skills. Append-only provenance logging, hash-locked dependencies, and a hardened
systemd unit. Validated end-to-end against real hardware (live M27 session; 766-sub M31 deep
stack through DeepSkyStacker).
