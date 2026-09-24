# Rig profile (optional)

Per-unit, per-site observations that are true of **your** Seestar and **your** sky — not of
Seestars in general. The run-book skills read this file at session start if it exists, and
work fine without it. Keep real coordinates out of anything you publish.

Fill in only what you have actually measured. "Unknown" is a valid, honest answer.

## Unit

- **Model / firmware:** e.g. Seestar S50, firmware 7.75
- **Mount mode:** alt-az (default) | EQ (wedge + polar align)
- **Measured pointing offset:** direction and magnitude, or "none observed".
  Only record a value here once you have classified it as *systematic* using the
  procedure in the `run-session` skill (persists across different sky angles AND
  survives a power-cycle + re-level + fresh dark alignment). Note how you measured it,
  e.g. "annotated centre ~170/1080 px vs frame centre 540 — measured on 3 targets at
  different sky angles".
  Any offset recorded before contract v1.3.0 (2026-09-24) includes J2000-vs-JNow
  precession of up to ~22′ on top of the real mechanical error — re-measure after the fix.
- **Focuser baseline:** typical position after the acquisition autofocus, if you track it.

## Site

- **Site name:** a label only. Set the real coordinates via `set_site_profile`, which
  keeps them in local data — do not write them here.
- **Bortle / sky brightness:** e.g. 8 (city)
- **Dark window:** how much astronomical dark you actually get in each season. At high
  latitude in summer this can be short or absent.
- **Known obstructions:** prefer the learned horizon mask (`suggest_horizon_mask` →
  `add_horizon_mask`) over prose. Note only what the mask cannot express, e.g.
  "neighbour's floodlight on a timer after midnight".

## Network / operations

- **Wi-Fi band used:** 2.4 GHz | 5 GHz, and whether the control link has been stable.
- **Data offload method and timing:** e.g. "share copy, after wind-down only" — never
  during a session; heavy transfers starve the scope's control link.
- **Anything else that has bitten you twice.** Once is an anecdote; twice is a rig quirk.
