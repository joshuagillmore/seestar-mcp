# Security

`seestar-mcp` is built for high-assurance operation. This document states the threat model,
maps the [OWASP MCP Top 10](https://owasp.org/) risks to the concrete controls in this repo,
gives the supply-chain runbook, and describes secrets handling. All controls below match
code that actually exists in `src/seestar_mcp/`.

## Threat model

### Assets

- **Telescope control** — the ability to command motion (goto, autofocus, park), change the
  filter/dew heater, and power the device down.
- **FITS data** — the RAW subs pulled off the device and the derived QA verdicts.
- **The provenance log** — the append-only audit trail of every tool call; its integrity is
  itself an asset (it is the chain of custody from photons to processed stack).
- **The RSA auth material** — the firmware-7.18+ challenge-response private key. Today
  `seestar_alp` owns the :4700 handshake, so this server does not need the key; if that ever
  changes, the key is the highest-value secret in the system.

### Trust boundaries

```
Claude app (phone)  ──TLS──▶  Anthropic API  ──TLS──▶  Jetson (outbound HTTPS ONLY)
                                                          │  Claude Code process
                                                          ▼
                                                     seestar-mcp  ──HTTP──▶  seestar_alp
                                                     (stdio child)          (127.0.0.1:5555)
                                                                                 │  LAN
                                                                                 ▼
                                                                            Seestar S50
```

1. **Claude app ↔ Anthropic API ↔ Jetson** — the phone reaches the session only via
   Anthropic's API. The Jetson makes **outbound HTTPS only** and opens no inbound ports.
2. **seestar-mcp ↔ seestar_alp** — a local, loopback HTTP call to `127.0.0.1:5555`. The MCP
   server is a stdio child of Claude Code; it opens no listening socket of its own.
3. **seestar_alp ↔ Seestar** — over the LAN (Alpaca :5555 fronting native JSON-RPC :4700 /
   data :80 / SMB :445), ideally on an isolated IoT VLAN with a DHCP reservation.

### Containerized topology (optional)

The optional Docker stack (`deploy/docker/`, see `deploy/docker/README-docker.md`) preserves these
boundaries: the MCP server still speaks **stdio with no listening socket** (launched as
`docker run -i`), reaching the bridge over a private `seestar_net` at `http://seestar-alp:5555`
instead of `127.0.0.1:5555`. The bridge publishes only the **SSC web UI to localhost** (`127.0.0.1:5544` — moved off `5432` to
avoid a postgres collision); the Alpaca port stays **network-internal** on `seestar_net` (not
host-published), so it can't collide with a native `seestar_alp` on the host. Caveat:
Docker only weakly approximates the systemd unit's egress allowlist / syscall filter / read-only
filesystem, so the **hardened systemd path remains the strongest-isolation option**; the container
stack is the alternative when co-tenancy or portability matters more than maximal egress control.

### Remote Control note

Because remote phone access is handled by Claude Code Remote Control (the Jetson makes
outbound HTTPS only), **there is NO inbound network surface to harden beyond the local
ports.** The residual remote-access concern is account security (protect the Claude
account/MFA — pairing the app grants session control), not a listening service. Every port
this project touches is loopback or LAN and is never exposed publicly.

### Autonomous operation

Unattended full-night operation (the `autonomous-night` skill) is **gated behind an explicit
user confirmation of a no-motion dry run.** `simulate_night` projects the whole night's
schedule and issues **no motion**; the first motion command only follows an explicit user
go-ahead. The hard guardrails — **dawn, battery, weather no-go, lost connection, max
duration** — **fail safe to `park`**: each hard stop winds the session down and parks the
mount, and if the scope's health can't be confirmed (device state unreadable → treated as
disconnected) the run stops. Every guardrail decision is **provenance-logged**. Autonomy
adds **no new network host and no inbound surface** — it is the same audited, honestly
described tools driven in a visible loop, still subject to every control below.

### Learned horizon mask

The horizon mask is **never auto-edited.** Obstruction inference (`log_sky_result` →
`suggest_horizon_mask`) only *suggests* arcs with evidence; the mask is changed solely by
an explicit user `add_horizon_mask` (or `set_site_profile`) after reviewing that evidence.
Inference is **weather-gated** (failures under a no-go sky are recorded weather-excluded
and never count as an obstruction) and **location-scoped** (records carry their lat/lon and
only aggregate near the queried site). The **GPS reconcile** on every plan prevents applying
a **stale mask at a new site** — if the scope has moved beyond the profile's tolerance the
saved mask is not applied and the mismatch is disclosed in the plan's `location` block. The
learner's state lives in `data/sky_failures.json` — **local, gitignored, no secrets**, no
new network host or dependency.

## OWASP MCP Top 10 — mitigations

| Risk | Our concrete control |
|---|---|
| **Tool poisoning** (hidden instructions in tool descriptions/schemas) | Tool descriptions are honest, human-readable, and non-obfuscated (see `server.py`); side effects are labelled `SIDE EFFECT` in plain English. Tools are statically defined in pinned code — **no dynamic/remote tool definitions**, no description mutation at runtime. |
| **Prompt injection via tool output** | Device telemetry and FITS data are treated as **data, not instructions**: tools return structured `dict`s, never executable text. The Skills explicitly instruct not to act on instructions embedded in device/FITS output. Every tool call and its result is recorded to the provenance log for after-the-fact review. |
| **Over-permissioned tools** | 34 single-purpose, least-privilege tools on `seestar-mcp` — each does exactly one thing. Motion/destructive tools (`goto_target`, `run_autofocus`, `set_filter`, `set_dew_heater`, `park`, `shutdown`, `download_subs`, `qa_session_report`) are clearly labelled, and the `run-session` / `anomaly-playbook` Skills gate motion and destructive operations behind **explicit user confirmation**. |
| **Supply chain** (rug-pulls / typosquats / drift) | Exact version pins in `pyproject.toml` and a **hash-locked `uv.lock`** (committed). An SBOM is generated via CycloneDX (`make sbom`), and a non-blocking `mcp-scan` check (`make scan`) watches for tool-poisoning / supply-chain findings. `seestar_alp` is **not** vendored — it is an external, operator-installed service this server drives over its local Alpaca HTTP API; operators should pin it to a reviewed release (it is primarily GPL-3.0 and is never bundled, linked, or redistributed here — see `NOTICE`). |
| **Prompt injection / insufficient sandboxing** | The daemon runs under a hardened systemd unit (`deploy/seestar-mcp.service`): a dedicated **unprivileged** user, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `RestrictNamespaces`, `LockPersonality`, `MemoryDenyWriteExecute`, `SystemCallFilter=@system-service`, and `ReadWritePaths` limited to the data/manifests dir. |
| **SSRF** (server tricked into fetching arbitrary URLs) | `data_client` only fetches from the **configured `seestar_host` on fixed ports** (:80 HTTP / :445 SMB) — there are **no user-controlled URLs**. The **only** external planning call is weather, and it has **two possible destinations**, both fixed URLs with no user-controlled host: `https://api.open-meteo.com` (HTTPS :443, keyless — the default), or `https://my.meteoblue.com` when `SEESTAR_METEOBLUE_API_KEY` is set (`resolve_source` picks meteoblue whenever a non-empty key is configured). The keyed path sends the key as an `apikey` query parameter, so **if you configure meteoblue, that key is a secret to protect and its host must be added to the egress allowlist instead of / in addition to Open-Meteo.** The key never reaches the provenance log — `provenance.redact`'s `_SECRET_KEY_RE` matches `key`, so both `apikey` and `meteoblue_api_key` are written as `***REDACTED***` (verified) — **nor stderr / journald**: httpx logs every request at INFO as `HTTP Request: GET <full-url> ...`, which would otherwise put `apikey=<key>` on the wire to Claude Code's MCP logs or journald on the Jetson, so `server.py` raises the `httpx`/`httpcore` loggers to WARNING immediately after `FastMCP(...)` and attaches a `logging.Filter` to the `httpx` logger that redacts any `apikey=<value>` that gets through regardless of level, as defense in depth against a future level change (both verified by `tests/test_logging_redaction.py`). SSRF posture is unchanged either way: both URLs are compile-time constants and only lat/lon (plus the key, when keyed) are parameterised. The systemd unit adds an IP egress allowlist (`IPAddressDeny=any` + `IPAddressAllow=localhost <SEESTAR_IP>`) so even a bug cannot reach anything but the Seestar and localhost; **when weather is enabled, add whichever host you actually use — `api.open-meteo.com` or `my.meteoblue.com` (:443) — to the allowed destinations.** Note `IPAddressAllow` pins IPs, not DNS names, so a DNS-named host like `api.open-meteo.com` cannot be IP-pinned reliably (its addresses rotate) — document it as the single allowed outbound weather destination, or resolve-and-pin at deploy time. A weather failure is non-fatal (the planner falls back to `go=None`, offline observability). |
| **Insecure updates** (firmware-fragile auth) | The firmware-7.18+ RSA handshake is isolated to `secrets.py` and single `# FIRMWARE-DEPENDENT` constants, so a firmware bump is a one-line key/constant swap in one place, not a code change. Dependencies are pinned; the auth/command map is the single updatable point. |
| **Insufficient logging** | Every tool call appends a structured record to an **append-only provenance JSONL** log (timestamp, tool, args, literal request, transaction IDs, response code, FITS hash), with recursive redaction of any sensitive field. Per-session manifests capture every command and QA verdict. |
| **Secrets exposure** | No secret ever enters config, source, or the audit log. `SecretStore` reads values on demand, never caches or logs them, and its `__repr__`/`status()` report only **presence** (`present`/`absent`), never values. The provenance layer redacts recursively. |
| **Network exposure** | All local ports are bound to **localhost/LAN, never public** (`bind_host` defaults to `127.0.0.1`). A `bind_host` field validator **warns** if it is ever set to a public/routable (non-loopback, non-RFC1918) address. The MCP server itself is stdio — it opens no listening socket at all. |

## Supply-chain runbook

The lockfile is **committed** — installs are reproducible and hash-pinned.

```bash
uv lock         # regenerate the hash-pinned lockfile (reproducible installs); commit it
make sbom       # write a CycloneDX SBOM (sbom.json) from the locked environment
make scan       # NON-BLOCKING mcp-scan supply-chain / tool-poisoning check
```

- `uv lock` produces `uv.lock` with per-dependency hashes; commit any change and review the
  diff (a surprising change is a signal to block the deploy).
- `make sbom` runs `cyclonedx-py environment` and emits a CycloneDX JSON SBOM artifact for
  diffing across builds.
- `make scan` runs `mcp-scan` and is **non-blocking** (leading `-` / `|| true`) so a missing
  tool or findings never fail the build; treat findings as review input, not a gate — until
  you choose to make it blocking in CI.

## Secrets handling

- The firmware-7.18+ RSA key and any tokens live in the gitignored `./secrets/` directory
  or in `SEESTAR_SECRET_*` environment variables. `.gitignore` already excludes `secrets/`,
  `*.pem`, `*.key`, and `.env`. **Secrets are never committed.**
- `secrets.py` (`SecretStore`) is the single load point. Values are read on demand and never
  cached, logged, or rendered by `__repr__`/`__str__`/`status()`.
- **Firmware updates (7.18+ RSA handshake) are a known, recurring breakage vector.** They
  are handled in exactly one place — `secrets.py` plus the `# FIRMWARE-DEPENDENT` constants
  — so recovering from a firmware bump is a one-line key/constant swap, never a code change.
  Today `seestar_alp` owns the :4700 handshake, so this server does not hold the key at all.

## Data access: the SMB / filesystem backend (host-wide caveat)

The optional filesystem backend (`SEESTAR_SEESTAR_IMAGE_ROOT`) reads subs directly off the
Seestar's SMB share via the OS redirector (a UNC path). The Seestar exposes that share as an
**unauthenticated guest with SMB signing**, which modern Windows refuses by default. Making
it reachable requires, on the client, an **elevated, machine-wide** relaxation:

```powershell
Set-SmbClientConfiguration -EnableInsecureGuestLogons $true   # revert with $false
```

**This is a real, host-wide security downgrade.** It re-enables unauthenticated, unsigned
SMB2/3 guest logons for the *whole machine*, exposing it to rogue-server / man-in-the-middle
file delivery from anything on the LAN — not just the Seestar. Treat it as opt-in and scoped:

- Prefer the **HTTP transport** (`download_subs` default) or a manual copy, which need **no**
  such change; only enable the guest relaxation if you specifically want the hands-free fs
  backend.
- If you do enable it, keep the Seestar on an **isolated IoT VLAN**, and **revert**
  (`... $false`) when you are done pulling data.
- The backend itself is still contained: listing names are basenamed and every write is
  fail-closed to the destination root (see the path-traversal control above); the risk is the
  OS-level SMB setting, not this code.

## Reporting

To report a security issue, please use **GitHub → the repository's Security tab → "Report a
vulnerability"** (private advisories), or email the maintainer at
**12974973+OrangeAgente@users.noreply.github.com** with `SECURITY` in the subject. Please do **not** open a public
issue for a vulnerability. Expect an initial acknowledgement within about a week; please allow
a reasonable window to develop and ship a fix before any public disclosure.

Any change that touches the auth path, the tool descriptions, the dependency set, or the
provenance/redaction logic must be reviewed against this document.
