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

## [0.1.0] - 2026-07-05

Initial build (pre-public): auditable `seestar-mcp` FastMCP server (33 tools) driving a ZWO
Seestar S50 via `seestar_alp`'s ASCOM Alpaca API; two-tier FITS QA; observing planner with
projects/history and a learned horizon mask; autonomous-night mode with hard guardrails; a
separate `seestar-refine` service (5 tools) for DeepSkyStacker / PixInsight stacking; and six
Claude Code skills. Append-only provenance logging, hash-locked dependencies, and a hardened
systemd unit. Validated end-to-end against real hardware (live M27 session; 766-sub M31 deep
stack through DeepSkyStacker).
