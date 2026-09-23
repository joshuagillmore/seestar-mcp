# Review remediation, tranche 1 (2026-09-22)

**Spec:** the 2026-09-22 whole-repo review (five reviewers: correctness, security/deploy,
CLAUDE.md invariants, skills↔tools contract, test suite). This plan fixes the seven
highest-priority findings. Each task below restates its finding, so the task text is the
authority. Everything else from the review is out of scope here.

**Branch:** `fix/review-2026-09-22`. One commit (or a small series) per task, each message
scoped to its task.

## Global Constraints

- Toolchain is `uv` only: `uv run pytest`, `uv run ruff check src tests`,
  `uv run python -c ...`. Never bare `python` or `pip`. Add no new dependencies.
- "Done" for every task: `uv run pytest` is fully green AND `uv run ruff check src tests`
  is clean. The baseline is 300 passed on commit faea725.
- Work TDD: write the failing test first, watch it fail for the right reason, then fix.
- Commit with `git -c core.autocrlf=false commit`. End every commit message with:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01JNous3HPkjboYUbfUrhAiT
  ```
- Determinism: nothing in `src/seestar_mcp/planning/` may read the clock. Only the tool
  layer (`server.py`) resolves "now" via `datetime.now(timezone.utc)`.
- Never-raise: controller methods and MCP tools return `{"ok": false, "error": ...}`
  instead of raising.
- Backward compatibility: additive fields and optional params must reproduce prior
  behavior exactly. Never remove or rename an existing output key; the SeeStar Console
  consumes them (see `tests/test_console_contract.py`).
- If a tool names a quantity in prose, it must also return that quantity as a field.
- Match the surrounding code's comment density and idiom. Comments explain *why*, citing
  the finding and date the way the existing comments do.
- Do not push. Do not touch `main`. Do not edit `.env`, `.env.enc` or `.sops.yaml`.

## Task 1: Isolate the test suite (no network, no real data dir, no env leakage)

**Finding:** the suite is not hermetic.
- `tests/test_server.py:120` (`_dummy_settings()` → bare `Settings()`) plus the goto tests
  make `goto_target` write `./data/run_state.json` into the REAL repo data dir. The file
  currently says "active M51", stamped by a test run.
- Astropy downloads IERS `finals2000A.all` at test time (`iers.conf.auto_download` is
  True by default, max age 30 days).
- `tests/test_planning_tools.py:604`
  (`test_guardrails_read_battery_from_device_state_without_a_second_call`) reaches the
  real Open-Meteo API, because `check_night_guardrails` calls weather and nothing mocks it.
  If `SEESTAR_METEOBLUE_API_KEY` were exported it would spend real meteoblue credits.
- Twelve `Settings(...)` constructions read the repo `.env` (pydantic-settings
  `env_file=".env"`, resolved against the cwd), and no test clears `SEESTAR_*` env vars.
- `tests/test_data_client.py:377` (`test_download_subs_windows_absolute_name_rejected`)
  passes on Linux CI for the wrong reason. On POSIX, `C:\Windows\evil.fits` is not
  absolute, so the guard does not fire. The code then attempts HTTP, then SMB, and
  smbprotocol's own `ValueError("Failed to connect…")` satisfies the bare
  `pytest.raises(ValueError)`. CI runs on both `ubuntu-latest` and `windows-latest`.

**Requirements:**
1. Create `tests/conftest.py` with autouse fixtures that, for every test:
   - `monkeypatch.chdir(tmp_path)`, so relative defaults (`./data`, `.env`) resolve into a
     throwaway dir. First check that no test depends on the repo root as cwd; fixtures
     are referenced via `Path(__file__)`, but verify.
   - delete every `SEESTAR_*` environment variable (monkeypatch.delenv) so the developer's
     shell cannot leak into tests.
   - block outbound network: any socket connect to a non-loopback address raises
     immediately with a clear message naming the address. respx-mocked httpx never opens
     sockets, so mocked tests are unaffected. Loopback must stay allowed.
2. In the same conftest (session scope or module import time), disable astropy IERS
   auto-download: `iers.conf.auto_download = False`. If astropy then errors on 2026+
   obstimes beyond the bundled IERS-B table, also set
   `iers.conf.iers_degraded_accuracy = "warn"`, or "ignore" if the warnings are noisy.
   Existing numeric assertions must still pass unchanged; the precision impact is
   sub-arcsecond.
3. Make `test_guardrails_read_battery_from_device_state_without_a_second_call` stub
   weather explicitly, so it no longer depends on the socket block to stay offline. Use
   the same mocking pattern other guardrail tests use (respx or monkeypatching
   `_weather_cached` / `assess_conditions_weather`); check how the neighbouring tests do it.
4. Rewrite `test_download_subs_windows_absolute_name_rejected` so it asserts the
   traversal guard's OWN rejection: match the guard's error message, and check the
   `data.download.rejected` provenance audit record the same way the test just above it
   (~line 370) does. It must pass identically on Windows and Linux. If the guard in
   `src/seestar_mcp/data_client.py` does not reject a drive-letter or backslash name on
   POSIX, make it reject such names on every platform. A name like `C:\Windows\evil.fits`
   is never a legitimate sub name from the Linux-based scope. Keep the existing
   rejection message style.
5. Add a test proving the socket guard works: a test that tries a non-loopback connect
   gets the guard's error.
6. After this task, a full `uv run pytest` must leave `git status` unchanged and must not
   modify or create anything under the repo's `data/` directory. Verify this by checking
   the mtime of `data/run_state.json` before and after the run. Do NOT delete or edit the
   existing `data/run_state.json`; it is the user's file.

## Task 2: Restore the unreachable per-session state init in `SeestarController.__init__`

**Finding:** commit df15a67 inserted `_weather_cached` in the middle of `__init__`. The
three per-session lines (`self.session_id = None`, `self.manifest = None`,
`self.target = None`, currently at `src/seestar_mcp/server.py:138-141`) now sit after
`return value` inside `_weather_cached`. They are dead code, so a fresh controller lacks
those attributes. `qa_session_report` (around `server.py:672`) then raises
`AttributeError: 'SeestarController' object has no attribute 'target'` on any server that
has not run `goto_target` in the same process, for example right after a restart.

**Requirements:**
1. Failing test first: a freshly constructed controller calling `qa_session_report(paths=[])`
   returns a dict (it must not raise). Also assert a fresh controller has `session_id`,
   `manifest` and `target` all `None`.
2. Move the per-session state lines back into `__init__` (after `self._weather_cache`),
   and remove the dead copy.
3. The `_weather_cached` docstring says "reused within ``qa_weather_cache_ttl_s``", but
   the setting is `weather_cache_ttl_s` (`config.py`). Fix the name.
4. Scan `server.py` for any other statement that is unreachable after a `return`/`raise`
   in the same block, and report what you find. Fix only exact siblings of this bug.

## Task 3: Stop the meteoblue API key leaking into logs

**Finding (reproduced):** `FastMCP("seestar-mcp")` (at `server.py:1685`, module import)
configures the root logger at INFO with a RichHandler on stderr. httpx logs every request
at INFO as `HTTP Request: GET <full url> ...`. The meteoblue request
(`planning/weather.py:342-357`) carries the key as the `apikey` query param, so every
keyed weather fetch writes `...&apikey=<KEY>` to stderr. That lands in Claude Code's MCP
logs, or journald on the Jetson. SECURITY.md (around line 95) says the key "never reaches
the provenance log … (verified)". That is true only of the provenance log.

**Requirements:**
1. Failing test first. Import `seestar_mcp.server`, so the FastMCP logging config is in
   effect, and set `caplog.set_level(logging.DEBUG)` on the root logger. Run a respx-mocked
   `MeteoblueSource("SUPERSECRETKEY123").assess(...)` and assert `"SUPERSECRETKEY123"`
   appears nowhere in `caplog.text`. Include a positive control proving caplog captures
   records from this path, for example that the test would see the key if the fix were
   absent, or that a WARNING-level httpx record is still captured. That way the test
   cannot pass vacuously.
2. Fix: in `server.py`, immediately after `mcp = FastMCP(...)`, raise the `httpx` and
   `httpcore` loggers to `logging.WARNING`. Add a short *why* comment citing this finding.
   Also add defense in depth: a `logging.Filter` that redacts `apikey=<value>` in log
   messages, attached to the `httpx` logger. A future level change must not re-open the
   leak. Keep it small.
3. Correct SECURITY.md's claim. Say the key is kept out of the provenance log by redaction
   AND out of stderr/journald by the httpx logger level plus the redaction filter, and
   name the test that pins it.
4. Add a CHANGELOG `Unreleased` entry under a Security heading, matching the existing
   CHANGELOG style. Recommend rotating the key if MCP or journald logs from before this
   fix have left the machine.

## Task 4: Fix the systemd unit's inline comments (seven hardening directives silently ignored)

**Finding:** `deploy/seestar-mcp.service` lines ~31-40 put trailing `# comments` on
directive lines, for example `ProtectSystem=strict          # entire filesystem…`. systemd
does not support inline comments: `#` only starts a comment at the beginning of a line.
The value becomes invalid and systemd drops the directive with only a log warning. That
affects `ProtectSystem`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`,
`ProtectKernelTunables`, `ProtectControlGroups` and `MemoryDenyWriteExecute`. Without
`ProtectSystem=strict`, `ReadWritePaths` confines nothing.

**Requirements:**
1. Failing test first: add `tests/test_deploy_unit.py`. It parses
   `deploy/seestar-mcp.service` and asserts that no directive line (`Key=value`, not
   starting with `#` or `;`) contains a `#` in its value. It also asserts that the seven
   directives above are present with their exact intended values (`strict`, `yes`, `yes`,
   `yes`, `yes`, `yes`, `yes`).
2. Move each trailing comment onto its own line directly above its directive. Keep the
   comment text.
3. Scan the other deploy files (`deploy/docker/*`, `deploy/dawn_park_watchdog.sh`) for the
   same inline-comment problem only where the format forbids it (systemd/ini-style), and
   report what you find.
4. Add a CHANGELOG `Unreleased` Security entry, and a note in the unit's header comment:
   verify with `systemd-analyze verify` and `systemd-analyze security seestar-mcp` after
   deploy.

## Task 5: Native error dicts must fail; expose the authoritative native mount state

**Finding:** `_native_error` (`server.py:1515`) only recognises a result *string* starting
with "Error", or a dict whose `"result"` is such a string. Seestar firmware reports
failures as a JSON-RPC dict, for example `{"error": "method not found", "code": 103}` (the
shape documented at `data_client.py:48-53`, observed on fw 8.46). That passes as success,
so `park`, `goto_target`, `start_stack`, `stop_view`, `set_filter`, `set_dew_heater`,
`run_autofocus`, `plate_solve` and `shutdown` return `ok: true` on a command the scope
rejected. `park()` then clears `run_state.json` (`server.py:480`) although the mount
never folded. Separately, no MCP tool returns the native `get_device_state` mount fields
that CLAUDE.md calls authoritative. Alpaca `/tracking` and `/atpark` disagree with the
device on this hardware, and `mount.close == True` means the arm is folded (parked). The
skills cannot confirm a park.

**Requirements:**
1. Failing tests first:
   - Parametrize over every controller method that routes a native result through
     `_native_fail`, with `method_sync` returning `{"error": "method not found", "code": 103}`.
     Each must return `ok: False`, with `error` containing `method not found`, and must
     not raise.
   - `park()` receiving that dict must NOT clear the run-state file. Write a run state
     first, then assert it still exists.
   - `park()` success path: `method_sync` returns a normal success reply (for example
     `{"jsonrpc": "2.0", "method": "scope_park", "result": 0, "code": 0}`), and the
     run-state file is cleared. This path is currently untested.
   - Pin the native method name each motion tool sends: `scope_park` for park,
     `pi_shutdown` for shutdown, `set_wheel_position` with `[position]` for set_filter, and
     the actual current names for `start_stack` and `stop_view` (read them from the code).
     Assert `method_sync.await_args`.
   - A success reply containing `"code": 0` and no `error` key must still be `ok: True`.
2. Extend `_native_error`. A dict with a truthy `"error"` key is an error. Return a
   string that includes the error text and, when present, the code, e.g.
   `"method not found (code 103)"`. Keep the existing string and nested-`result`-string
   handling. Update the docstring with the fw 8.46 shape and the date.
3. Add the native mount state to `get_status` as ADDITIVE fields. `get_status` also calls
   `method_sync("get_device_state")` best-effort, and adds `"mount_parked"` (from
   `result.mount.close`) and `"mount_tracking"` (from `result.mount.tracking`). Each is
   `bool` or `None`. Any failure or unexpected shape yields `None` for both, and never
   makes `get_status` fail. Write a small pure parser `_parse_mount_state(dev) ->
   tuple[bool | None, bool | None]`, placed next to `_parse_device_health`. It should
   tolerate the nested `result.mount` shape and a flat `mount` dict for mocks. Keep all
   existing `get_status` keys and values unchanged. Test the parser with a
   realistic fw 8.46-shaped payload (`{"jsonrpc": "2.0", "method": "get_device_state",
   "result": {"mount": {"close": true, "tracking": false, ...}, ...}, "code": 0}`) and with
   junk input. Test that get_status carries both fields.
4. Update the `get_status` tool's docstring/description (the `@mcp.tool` wrapper around
   `server.py:1711`). Say that `tracking` is Alpaca's view and is known to disagree with
   the device, and that `mount_parked`/`mount_tracking` are the authoritative native
   fields. The description must say it now makes one native `get_device_state` call.
5. Keep the console contract test green. Its required keys are a subset, so additive is
   fine.

## Task 6: `dark_window` clips or picks the wrong night; planning tools plan the wrong night

**Finding (reproduced at lat 38.9, lon -77.0):** `dark_window`
(`src/seestar_mcp/planning/astro.py:178`) builds a ±12 h sun grid centred on `when`,
takes the darkest sample, and expands the contiguous sub-−18° span *within that grid*.
Results:
- `when=2026-09-22T12:00Z` (08:00 EDT) → `00:40–09:25Z Sep 22`, i.e. LAST night.
- `when=2026-09-22T16:30Z` (12:30 EDT) → `04:30–09:25Z Sep 22`: last night, clipped at the
  grid edge.
- `when=2026-09-22T20:00Z` (16:00 EDT) → `00:35–08:00Z Sep 23`: tonight, but dawn clipped
  to 08:00Z (true astronomical dawn ≈ 09:25Z).
- `when=2026-09-23T02:00Z` (in the dark) → `00:35–09:25Z Sep 23`, which is correct.
The tool layer passes `when = date or now` straight into `dark_window`,
`moon_illumination` and `rank_targets` (which calls `observability` → `dark_window`
internally). So `assess_conditions`, `plan_targets`, `get_target_observability` and
`simulate_night` plan the wrong or a truncated night whenever they are called outside the
dark. That covers morning and afternoon planning, which is the normal case. A bare date
like `date="2026-09-22"` parses to 00:00Z (20:00 EDT on the 21st), so it also plans the
night of the 21st→22nd. Observers mean the night that BEGINS on the evening of the 22nd.

**Design (binding):**
1. **`dark_window` keeps its "nearest night" semantics but is never clipped.** Choose
   the darkest sample within ±12 h of `when`, exactly as now, so the same night is
   selected. Then expand the contiguous span on a grid wide enough that the span cannot
   hit an edge: build the grid over ±24 h, restrict the argmin to the central ±12 h, and
   expand on the full grid. Keep the high-latitude "never astro-dark" fallback.
   `check_night_guardrails` relies on nearest-night semantics and must keep it. After
   dawn (e.g. 09:40Z) it must still get the JUST-ENDED night, so the dawn stop fires.
   Before dusk (e.g. 23:30Z) it must get tonight.
2. **New pure function in `astro.py`: `planning_when(site, when_utc) -> str`.** It
   returns the instant the planning tools should use as `when`:
   - If `when_utc` is a bare date (`YYYY-MM-DD`), anchor it to local mean solar noon of
     that date: `date 12:00 UTC − lon_deg/15 hours` (lon east-positive, so lon −77 gives
     ≈17:08Z). Continue with that instant.
   - If the instant lies inside the window `dark_window(site, instant)` returns
     (dusk ≤ instant ≤ dawn), return it unchanged, as an ISO UTC string.
   - Otherwise return the time of the NEXT solar minimum (the lowest Sun altitude on a
     grid from the instant to the instant + 24 h). That lies inside the upcoming night's
     window, so `dark_window`, `observability` and `moon_illumination` evaluated there all
     agree on the upcoming night.
   - Must be idempotent: `planning_when(site, planning_when(site, x)) ==
     planning_when(site, x)`. This also holds for the high-latitude fallback, where the
     solar minimum lies inside the fallback span.
   - Pure: no clock reads. Never raises for valid ISO input.
3. **Tool layer:** in `assess_conditions`, `get_target_observability`, `plan_targets`
   and `simulate_night` (`server.py` ~871-1195), resolve
   `when = planning_when(site, date or now_iso)` AFTER loading the site, and use that
   `when` everywhere the method used the old one. Reorder the site load before it where
   needed; the no-site error path must stay the same. `simulate_night` passes `date=when`
   into `plan_targets`, which is safe because of idempotency. Leave
   `check_night_guardrails` on `dark_window(site, now)`. Update each tool's
   description/docstring for `date`: "a bare `YYYY-MM-DD` means the night beginning on
   that date's evening; omitted means tonight (or the current night if already dark)".

**Tests (write first, at lat 38.9, lon -77.0, elevation 0; allow ±5 min tolerance on
times because the grid step is ~5 min):**
- `dark_window` at 20:00Z Sep 22 → dawn ≈ 09:25Z Sep 23 (not 08:00Z).
- `dark_window` at 16:30Z Sep 22 → the full 00:40–09:25Z Sep 22 window (not clipped at 04:30Z).
- `dark_window` at 09:40Z Sep 23 → the just-ended night (dawn ≈ 09:25Z Sep 23; dawn < when).
- `dark_window` at 23:30Z Sep 22 → tonight (dusk ≈ 00:35Z Sep 23).
- `dark_window` at 02:00Z Sep 23 → unchanged from today's output.
- `planning_when` at 12:00Z Sep 22 → the result's `dark_window` starts ≈ 00:35Z Sep 23.
- `planning_when` at 02:00Z Sep 23 → returned unchanged.
- `planning_when("2026-09-22")` → its `dark_window` starts ≈ 00:35Z Sep 23.
- Idempotency, including a high-latitude summer site (e.g. lat 65, June 21) where
  astro-dark never occurs.
- A tool-level test: `plan_targets(date="2026-09-22")` (or `assess_conditions`, whichever
  is cheaper to mock) reports `dark_window_utc` for the night beginning Sep 22 evening.
- All existing astro, ranker, planning-tools and console-contract tests stay green
  without changing their assertions. If one must change, stop and report why instead.

## Task 7: Compute the planning night once per ranking, and surface it

**Finding (introduced by Task 6, measured by its reviewer):** `rank_targets`
(`planning/ranker.py` ~line 330) calls `observability(site, target, when)` once per catalog
target (120), and each call recomputes `dark_window(site, when)` inside `_observability`
(`planning/astro.py`). Task 6 widened the sun grid to ±24 h. That took `dark_window` from
33 to 64 ms and 120 × `observability` from 8.1 to 11.1 s on a desktop (+37%). The Jetson
that runs this from a phone is slower. Computing the window once and passing it in
measured **3.98 s, with byte-identical results**. Separately, `plan_targets` and
`get_target_observability` do not report which night they planned. Since Task 6,
`date=None` re-anchors to the upcoming night, so a caller cannot confirm the night from the
output. That breaks the rule "if a tool names a quantity, return it as a field".

**Requirements:**
1. `observability(site, target, when_utc, dark_window_utc=None)` gets an ADDITIVE, optional
   parameter. When it is given (a `(dusk_iso, dawn_iso)` pair), `_observability` uses it
   and does not call `dark_window`. When it is `None`, behaviour is exactly as today.
2. `rank_targets` computes `dark_window(site, when)` ONCE per call and passes it to every
   `observability` call. Its public signature stays unchanged, or changes only additively.
3. Tests, written first:
   - A regression test proving `rank_targets` output is identical with and without the
     precomputed window: for a fixed site, instant and small catalog slice, compare
     against per-target `observability(..., dark_window_utc=None)`.
   - A test that `dark_window` is called exactly once per `rank_targets` call (count it
     with monkeypatch).
   - A test that `observability(..., dark_window_utc=None)` still matches its old output.
4. `plan_targets` and `get_target_observability` gain an ADDITIVE top-level
   `dark_window_utc` field: the `(dusk, dawn)` pair of the night they actually planned, in
   the same ISO format `assess_conditions` already returns. Test both. `simulate_night`
   already returns `dark_window_utc`; check that it is unchanged.
5. Measure before and after in the report: 120 × `observability` via `rank_targets`, and one
   `plan_targets` call with weather mocked.
6. Nothing in `planning/` may read the clock. All existing assertions stay unchanged.

## Task 8: Skills: fail-closed guardrail, native park confirmation, autonomous weather-stop precedence

**Findings:**
- `skills/autonomous-night/SKILL.md` Phase B step 1 (~lines 113-118) goes to Phase C only
  on `action: "park_and_stop"`, and "Otherwise" proceeds to the next target.
  `check_night_guardrails` returns `{"ok": false, "error": ...}` with NO `action` key when
  there is no site profile or it hits an exception (`server.py` ~1226, ~1270), so the
  check FAILS OPEN.
- The skills require confirming that motion succeeded (e.g. park in autonomous-night
  Phase C around line 149, and run-session around lines 17 and 327), but gave no way to
  check a park. Task 5 added `get_status` fields `mount_parked` / `mount_tracking` (native
  `get_device_state` `mount.close` / `mount.tracking`, authoritative; Alpaca `tracking`
  is not). Task 5 also made native error dicts return `ok: false`.
- Contradiction: `skills/anomaly-playbook/SKILL.md` (~lines 72-76 and 157-161) says always
  ask the user before pausing or parking and never auto-abort on weather. autonomous-night
  sends mid-slot weather flips to the playbook (~139-142) but also says hard stops are
  non-negotiable and park (~106-108, ~165-168). An unattended agent can block on a
  question nobody answers while rain arrives.
- The anomaly-playbook's clouds-vs-tracking branch (~28-33) says to re-check tracking via
  `get_status`, but that reads Alpaca `tracking`, which is wrong on this hardware.
- Task 6 changed planning-tool `date` semantics: a bare `YYYY-MM-DD` means the night
  beginning that evening, and an omitted date plans tonight even when called in the
  morning or afternoon.

**Requirements (skills and docs only, no code):**
1. autonomous-night Phase B step 1: proceed ONLY when the result has `ok: true` AND
   `action == "continue"`. Anything else (`ok: false`, a missing `action`, any other
   action) goes straight to Phase C, quoting the error or hard-stop reason. State that
   explicitly as a fail-closed rule.
2. autonomous-night Phase C: park first, then confirm with `get_status` that
   `mount_parked` is `true`. If the park returns `ok: false`, or `mount_parked` is not
   `true` after a reasonable wait (poll a few times over ~1-2 min), retry park once. If it
   still fails, alert the user loudly (a push notification if available) and say the
   dawn watchdog is the backstop. Keep the rest of Phase C's order sensible
   (`stop_view` → `park` → confirm → log).
3. anomaly-playbook: add an explicit precedence rule. During a user-authorised autonomous
   night (the autonomous-night skill's pre-authorisation), a guardrail hard stop or
   weather no-go parks WITHOUT asking. The "always ask" rules apply only to attended
   sessions. Also change its tracking check to use `get_status.mount_tracking` (native),
   and say Alpaca `tracking` is unreliable on this hardware.
4. run-session: wherever it says to confirm a motion command succeeded, name the concrete
   signal. Every motion tool now returns `ok: false` on a native error, including
   `{"error": ..., "code": ...}` replies. For park, confirm `get_status.mount_parked ==
   true`. Fix any claim that `run_autofocus` "errors on unsupported firmware" so it
   matches the new behaviour: an unsupported method now returns `ok: false`, "method not
   found (code 103)".
5. observing-planner (and any other skill that describes `date`): document the new
   `date` semantics from Task 6.
6. Keep each skill's existing voice and structure. Make minimal, surgical edits and don't
   rewrite sections. Do not change thresholds or procedures beyond these items.
7. Keep the docs in step with Tasks 5-7:
   - `docs/CONTRACT.md`: MINOR bump to **v1.2.0** (its rule 3: adding a key is MINOR).
     Update the title, the Version row and the Changelog, following the existing entry
     style. Record:
     - Task 5's `get_status` keys `mount_parked` / `mount_tracking` (bool or null, from
       native `mount.close` / `mount.tracking`).
     - Task 7's `dark_window_utc` on `plan_targets` and `get_target_observability`.
     - Task 6's change to how `date` is read on the covered planning tools: a bare date is
       the night beginning that evening, and an omitted date plans the upcoming night. No
       key, unit or frame changed.
     If the contract lists `get_status` keys anywhere, add the two new keys there as well.
   - `skills/run-session/SKILL.md` (~line 343) says `get_status` makes 5 device calls. It
     now makes 6, because Task 5 added one native `get_device_state` call.
   - `CLAUDE.md`: make the test count in the Toolchain section ("300 tests") match
     `uv run pytest --collect-only -q | tail -1` at the time you edit it.
   - Optional, one line in observing-planner if it fits naturally: a project's "imaged N
     days ago" is now counted from the planned night, not the wall clock.
