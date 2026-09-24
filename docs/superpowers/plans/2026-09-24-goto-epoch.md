# Goto coordinate epoch: J2000 → JNow (2026-09-24)

**Spec (finding, strong evidence, pending live confirmation):** objects imaged through the MCP land 8–23′ off-centre, while the phone app centres them. `goto_target` sends J2000 catalog coordinates (`iscope_start_view` → `target_ra_dec: [ra/15, dec]`, server.py ~line 364), but the Seestar works in current-epoch coordinates (JNow). Precession since J2000 is about 26.7 years. The offsets measured on the night of 2026-09-23/24 (fw 8.46) match the predicted J2000→JNow precession shift:

| Target | Predicted shift | Measured in frame |
|---|---|---|
| M92 | 9.1′ | 9.2′ |
| M57 | 12.7′ | 12.6′ |
| M1 | 22.4′ | 22.5′ |
| M13 | 11.9′ | 8.2′ |

The M13 measurement comes from the first goto of the night on a large cluster, so its annotation centre is less precise. The same mechanism explains the earlier mystery. The solver's `ra_dec` and the FITS header `RA`/`DEC` sat "near the commanded target". They are most likely the TRUE field centre expressed in JNow, which numerically equals the J2000 numbers we sent, because the scope treated them as JNow. Docs written earlier today say "not the field centre", and that is wrong.

**Branch:** `fix/goto-epoch` from `main` @ 9647eca, in a sibling worktree. Never edit the main checkout; live MCP servers load code from it.

## Global Constraints
- **Toolchain:** `uv` only. No new dependencies; astropy is already pinned. "Done" means `uv run pytest` is green AND `uv run ruff check src tests` is clean. Baseline: 496 passed.
- **TDD:** write the failing test first.
- **Committing:**
  - Commit with `git -c core.autocrlf=false commit` and the session trailers.
  - Check `git show HEAD:<file> | tr -cd '\r' | wc -c` is 0 for every touched file. The Edit tool on this machine writes CRLF.
  - Don't push, and don't touch `main`.
- **Determinism:** nothing in `planning/` reads the clock. Conversion helpers take an explicit time, and only `server.py` supplies `datetime.now(timezone.utc)`.
- **No network:**
  - Use `FK5(equinox=<time>)` (mean equator and equinox of date: precession only). It is analytic and needs no IERS download. That matters on the air-gapped Jetson and under the test suite's offline IERS configuration.
  - Do NOT use TETE, CIRS or AltAz for this conversion.
  - Nutation and aberration (under 1′) are out of scope. The live test will show the residual.
- **Compatibility:** additive only for output keys. Never rename or remove one.
- **Quantities:** if a tool names a quantity in prose, it also returns it as a field.
- **Privacy:** use synthetic test sites only. Catalog target coordinates are fine. Never write real-site coordinates or place names.

## Task 1: Convert J2000 → JNow for the goto, and JNow → J2000 for solve results

**Requirements:**
1. **Pure helpers** in `src/seestar_mcp/planning/astro.py`:
   - `j2000_to_jnow(ra_deg, dec_deg, when_utc) -> (ra_deg, dec_deg)`
   - `jnow_to_j2000(ra_deg, dec_deg, when_utc) -> (ra_deg, dec_deg)`

   Both use `FK5(equinox=Time(when_utc))`. Both are deterministic in `when_utc`, and both are exact inverses to better than 0.1″. Normalise RA to [0, 360).
2. **Tests, written first.** For M1 (J2000 RA 83.633°, Dec 22.017°) at `2026-09-24T04:00:00Z`, JNow is RA ≈ +22.4′/cos(Dec) east (ΔRA·cosδ ≈ +22.4′) and ΔDec ≈ +1.0′. Assert to about 0.2′.
   - Add a round-trip test.
   - Add a polar or high-dec sanity case, e.g. Dec 85°, where it must not raise.
   - Add an RA wrap near 359.9° / 0°.
3. **`goto_target`** (`server.py` ~line 300-380):
   - Convert the caller's J2000 `ra`/`dec` (degrees) to JNow at the real current time, THEN convert RA to hours for `target_ra_dec`. The caller's contract (J2000 degrees) is unchanged.
   - Return the additive fields `ra_jnow_deg`, `dec_jnow_deg` and `epoch_utc` (the instant used). Keep the existing `ra`/`dec` fields as the caller's J2000 values.
   - Test that the `method_sync` params carry `target_ra_dec == [ra_jnow/15, dec_jnow]` for a pinned clock. Follow how other tool tests pin "now".
   - Test that an existing goto test's expectations are updated deliberately (JNow numbers), and not loosened.
4. **`plate_solve`** (additive): add `ra_j2000_deg` and `dec_j2000_deg`. Convert the solver's reported JNow centre (`ra_deg`/`dec_deg`) with `jnow_to_j2000` at the real current time. If the solve has no position, both are null.
5. **`get_view_state.stack`** (additive): add `solve_ra_j2000_deg` and `solve_dec_j2000_deg`.
   - Compute them in `server.py`'s `get_view_state` after `_summarize_view_state`, which stays pure in `native_reply.py`.
   - They are null when the solve position is null.
   - The slot watcher needs no change.
6. **Rewrite the docstrings and tool descriptions** that currently say the solve coordinates are "NOT the field centre" (`plate_solve`, `get_view_state`, `_extract_solve_fields`, `native_reply._summarize_view_state`). New meaning: they are the solved field centre in JNow (equinox of date), and the `*_j2000_deg` fields are the same point in J2000 for comparison with the catalog. Cite this finding and date.

## Task 2: Docs, contract and skills follow the epoch finding

**Requirements:**
1. **`docs/CONTRACT.md`.** v1.2.0 is now RELEASED on `main`, so add a **v1.3.0** entry, MINOR: title, Version row and Changelog, in the existing style.
   - List the new keys: goto `ra_jnow_deg`/`dec_jnow_deg`/`epoch_utc`, plate_solve `ra_j2000_deg`/`dec_j2000_deg`, and stack `solve_ra_j2000_deg`/`solve_dec_j2000_deg`.
   - CORRECT the v1.2.0 prose that said solve coordinates are "not the field centre". They are the field centre in JNow.
   - Note the behaviour change: `goto_target` now precesses J2000 to JNow before slewing, and the input contract is unchanged.
   - If the contract tests pin the version literal (`tests/test_console_contract.py`, `assert version == ...`), update it in the same commit, as earlier bumps did. The test count row must stay accurate.
2. **`CHANGELOG.md` Unreleased:** add a Fixed entry for the precession fix, citing the measured table without site details.
3. **`CLAUDE.md` gotchas:**
   - Add: "`goto_target` takes J2000; the firmware expects JNow — the server precesses (live test 2026-09-24: 9–22′ offsets matched precession)."
   - Correct any "solve coordinates are not the field centre" text.
   - Update the test count to match collection.
4. **Skills** (`run-session`, `autonomous-night`, `anomaly-playbook`):
   - Correct every "solve coordinates / FITS RA-DEC are not the field centre" statement. They are the JNow field centre, and `*_j2000_deg` compares with the catalog.
   - The "~20–30′ systematic frame offset" calibration note: after this fix, expect it to shrink to the mount's residual pointing error, a few arcminutes. Mark the old note as SUPERSEDED, keeping the history in one line: "it was J2000-vs-JNow precession, not hardware".
   - Keep the "classify before reacting" framing table. Keep Annotate `target_px` as the framing measure.
   - Keep edits surgical and in each skill's voice. Verify every name against the code at HEAD.
