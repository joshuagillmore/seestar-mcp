"""Guard against inline `#`/`;` comments on systemd directive lines.

Added 2026-09-22: a review found `deploy/seestar-mcp.service` had trailing
`# comment` text on seven hardening directives (`ProtectSystem`, `ProtectHome`,
`PrivateTmp`, `PrivateDevices`, `ProtectKernelTunables`, `ProtectControlGroups`,
`MemoryDenyWriteExecute`). systemd only treats `#`/`;` as a comment marker at
the START of a line — mid-line, it becomes part of the directive's value, which
systemd rejects with a log warning and silently drops the directive. Without
`ProtectSystem=strict` in particular, `ReadWritePaths` confines nothing. This
test parses the real unit file (not a copy) so a future edit can't reintroduce
the bug unnoticed.

Uses `Path(__file__)`, not the cwd: `tests/conftest.py` chdirs every test into
its own tmp_path.

Fix round 1 (2026-09-22, review of commit 5c5e6ab): that fix accidentally
converted the whole unit file to CRLF. The unit deploys to a Linux Jetson,
where `core.autocrlf` doesn't apply; systemd < v246 (JetPack 5.x ships 245)
does not strip a trailing `\r` from a directive's value, so every directive
would silently carry one — the exact same corruption class this file exists
to catch, just spread across the whole unit instead of seven lines. The
original inline-comment test couldn't see it because `Path.read_text()` does
universal-newline translation, which absorbs `\r` before `splitlines()` runs.
`test_unit_file_is_lf_only` reads raw bytes to close that gap.
"""

from __future__ import annotations

from pathlib import Path

UNIT_PATH = Path(__file__).resolve().parents[1] / "deploy" / "seestar-mcp.service"

# The seven directives the review flagged, with the value each is meant to have.
EXPECTED_DIRECTIVES = {
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "PrivateTmp": "yes",
    "PrivateDevices": "yes",
    "ProtectKernelTunables": "yes",
    "ProtectControlGroups": "yes",
    "MemoryDenyWriteExecute": "yes",
}


def _parse_directives(text: str) -> dict[str, str]:
    """Minimal systemd-unit parser: just enough for this file.

    Comment lines are ones whose first non-whitespace character is `#` or `;`.
    Section headers (`[Service]`) and blank lines are skipped. Everything else
    is a `Key=value` directive line (no backslash continuation — the unit
    doesn't use it, confirmed by grep before writing this test).
    """
    directives: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        key, _, value = line.partition("=")
        directives[key.strip()] = value.strip()
    return directives


def test_unit_file_exists() -> None:
    assert UNIT_PATH.is_file(), f"expected unit file at {UNIT_PATH}"


def test_unit_file_is_lf_only() -> None:
    """Raw-byte check: `\\r` anywhere means CRLF snuck in.

    Deliberately reads bytes, not `.read_text()` — Python's universal-newline
    translation would absorb a `\\r` before it ever reached a string-based
    assertion, exactly what let commit 5c5e6ab's CRLF conversion slip past the
    other tests in this file.
    """
    raw = UNIT_PATH.read_bytes()
    assert b"\r" not in raw, (
        "deploy/seestar-mcp.service contains CRLF line endings; the unit "
        "deploys to a Linux Jetson where a trailing \\r on a directive value "
        "is not stripped by systemd < v246 and corrupts every directive"
    )


def test_no_directive_value_contains_inline_comment() -> None:
    """systemd doesn't support inline comments: a mid-line `#` becomes part of
    the value, not a comment. No directive's value may contain one."""
    directives = _parse_directives(UNIT_PATH.read_text(encoding="utf-8"))
    offenders = {key: value for key, value in directives.items() if "#" in value}
    assert not offenders, f"directive values still contain inline '#' comments: {offenders}"


def test_hardening_directives_present_with_exact_values() -> None:
    directives = _parse_directives(UNIT_PATH.read_text(encoding="utf-8"))
    for key, expected_value in EXPECTED_DIRECTIVES.items():
        assert key in directives, f"missing directive: {key}"
        assert directives[key] == expected_value, (
            f"{key}={directives[key]!r}, expected {expected_value!r}"
        )
