"""List and download RAW FITS subs from a ZWO Seestar S50.

Firmware-fragility isolation (READ THIS)
----------------------------------------
The Seestar's raw JSON-RPC control port (:4700) gained a **mandatory RSA
challenge-response handshake in firmware 7.18+** (get_verify_str -> sign
challenge -> verify_client -> pi_is_verified). That auth path is a known,
recurring breakage vector. To keep the firmware-fragile surface confined to
ONE place, this module does **not** open its own :4700 socket. Instead, sub
*listing* is routed through the injected :class:`AlpacaClient.method_sync`, so
``seestar_alp`` owns the fragile handshake/auth for us.

*Downloads* are plain, unauthenticated file transfers and therefore go direct:
- **HTTP (:80)** via the device's built-in web server is the default.
- **SMB (:445)** is the automatic fallback when HTTP fails.

Because several device-side specifics are only known from community
reverse-engineering (not confirmed against real hardware), every such value is
funnelled into a single default and marked ``# FIRMWARE-DEPENDENT`` so it can
be corrected in exactly one place once validated on a device:
- the native listing method name (``list_method``),
- the JSON-RPC params *shape* for that method,
- the SMB share name (``smb_share``).

This module holds no secrets; provenance is logged for the listing and for each
downloaded sub (with a sha256 hash binding the file into the chain of custody).
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from .provenance import hash_fits

if TYPE_CHECKING:
    from .alpaca_client import AlpacaClient
    from .config import Settings
    from .provenance import ProvenanceLog

# FIRMWARE-DEPENDENT: native JSON-RPC method used to enumerate saved subs.
#
# PROBED AND ABSENT on firmware 8.46 (2026-08-03): this name and nine other
# plausible spellings (get_file_list, get_img_list, get_image_list,
# get_album_list, list_img_files, get_files, get_stacked_img_list,
# get_img_name_list, get_sub_list) all answer ``{"error": "method not found",
# "code": 103}`` against a working control call. There is most likely no native
# listing RPC at all — the app enumerates over the file share.
#
# This costs nothing in practice: ``list_subs`` prefers the filesystem/SMB path
# whenever ``image_root`` is configured, which is the validated route (697 subs
# for M76 read that way on 8.46). This constant is only reached when no image
# root is set. Kept, and overridable via ``list_method``, in case a future
# firmware adds one.
DEFAULT_LIST_METHOD = "get_img_file_list"

# FIRMWARE-DEPENDENT: SMB share exposing the eMMC image store. Best guess from
# community reports; validate against a real device and update here only.
DEFAULT_SMB_SHARE = "EMMC Images"

# Keys under which a device might wrap a file list inside a dict response.
_LIST_WRAPPER_KEYS = ("files", "list", "result")
# Keys under which a per-file dict might carry its display name.
_NAME_KEYS = ("name", "filename")

_FITS_SUFFIXES = (".fit", ".fits")
_TARGET_SUB_RE = re.compile(r"^(?P<target>.+)_sub$", re.IGNORECASE)


@dataclass
class SubInfo:
    """A single RAW sub-frame the device has saved."""

    name: str  # filename, e.g. "DSO_Stacked_12_M31_10s_20260704_2231.fit"
    path: str  # device-relative path for download, e.g. "M31_sub/DSO_...fit"
    size: int | None = None
    target: str | None = None


def _safe_name(raw: str) -> str:
    """Reduce an UNTRUSTED listing name to a bare, separator-free filename.

    The device listing is untrusted (a spoofed or MITM'd Seestar can return
    arbitrary entries), so a name may contain path separators, ``..`` segments,
    or an absolute path/drive. We normalize backslashes to forward slashes
    first (so Windows-style ``..\\..\\x`` is neutralized even on POSIX), take
    the final path component, and strip any residual leading separators or
    drive. Returns "" for an empty / ``.`` / ``..`` result so the caller can
    reject it. The result can never contain a directory separator.
    """
    candidate = os.path.basename(raw.replace("\\", "/")).strip()
    # Defensive: strip any residual leading separators / drive letter remnant.
    candidate = candidate.lstrip("/\\")
    if ":" in candidate:  # e.g. a bare "C:" style remnant
        candidate = candidate.rsplit(":", 1)[-1]
    if candidate in ("", ".", ".."):
        return ""
    return candidate


def _has_windows_path_syntax(name: str) -> bool:
    """True if ``name`` holds a backslash or a Windows drive (``C:``, ``\\\\host``).

    The scope's filesystem is Linux, so no genuine sub name carries either. But
    pathlib only reads them as path syntax on Windows: ``C:\\Windows\\evil.fits``
    escaped the data dir there yet was one harmless filename on POSIX, so the
    containment guard gave a different answer per platform (2026-09-22 review).
    A pure string check refuses them everywhere.
    """
    return "\\" in name or bool(PureWindowsPath(name).drive)


def _is_fits(name: str) -> bool:
    return name.lower().endswith(_FITS_SUFFIXES)


def _infer_target(path: str) -> str | None:
    """Infer a target name from a ``<Target>_sub`` segment of ``path``."""
    for segment in re.split(r"[\\/]", path):
        match = _TARGET_SUB_RE.match(segment)
        if match:
            return match.group("target")
    return None


def _normalize_target(name: str) -> str:
    """Reduce a target/dir name to a comparison key: lowercase alphanumerics only.

    So ``"M 31"`` -> ``"m31"`` and the targets ``"M31"``/``"M 31"``/``"m31"`` all
    normalize to the same key, letting a spaced on-scope target name match a
    ``"<Target>_sub"`` directory regardless of spacing/case.
    """
    return "".join(ch for ch in name.lower() if ch.isalnum())


class DataClient:
    """Lists FITS subs (via seestar_alp) and downloads them (HTTP, SMB fallback)."""

    def __init__(
        self,
        alpaca: AlpacaClient,
        *,
        host: str,
        http_port: int = 80,
        smb_port: int = 445,
        http_timeout_s: float = 30.0,
        provenance: ProvenanceLog | None = None,
        http_client: httpx.AsyncClient | None = None,
        list_method: str = DEFAULT_LIST_METHOD,
        smb_share: str = DEFAULT_SMB_SHARE,  # FIRMWARE-DEPENDENT (see module docstring)
        image_root: str = "",
    ) -> None:
        self._alpaca = alpaca
        self._host = host
        self._http_port = http_port
        self._smb_port = smb_port
        self._provenance = provenance
        # When set, list/download read subs directly from this filesystem path
        # (UNC/mapped/local) via the OS SMB redirector instead of JSON-RPC/HTTP.
        self.image_root = image_root
        # FIRMWARE-DEPENDENT: single updatable point for the listing method name.
        self.list_method = list_method
        self._smb_share = smb_share
        # Default download destination; only set when built via ``from_settings``.
        self._data_dir: Path | None = None

        if http_client is None:
            self._http = httpx.AsyncClient(timeout=http_timeout_s)
            self._owns_http = True
        else:
            self._http = http_client
            self._owns_http = False

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        alpaca: AlpacaClient,
        provenance: ProvenanceLog | None = None,
    ) -> DataClient:
        """Build a :class:`DataClient` from a :class:`Settings` object."""
        client = cls(
            alpaca,
            host=settings.seestar_host,
            http_port=settings.http_port,
            smb_port=settings.smb_port,
            http_timeout_s=settings.http_timeout_s,
            provenance=provenance,
            image_root=settings.seestar_image_root,
        )
        client._data_dir = settings.data_dir
        return client

    # --- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying httpx client, but only if we created it."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> DataClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # --- internals ---------------------------------------------------------

    def _log(self, **kwargs: Any) -> None:
        if self._provenance is not None:
            self._provenance.log_call(**kwargs)

    @staticmethod
    def _coerce_to_list(raw: Any) -> list[Any]:
        """Normalize a tolerant listing response into a flat list of items."""
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in _LIST_WRAPPER_KEYS:
                value = raw.get(key)
                if isinstance(value, list):
                    return value
        return []

    @staticmethod
    def _coerce_item(item: Any) -> SubInfo | None:
        """Turn a single listing entry (str or dict) into a SubInfo, or None.

        The device listing is UNTRUSTED. Both branches route the display name
        through :func:`_safe_name`, so ``SubInfo.name`` can never contain a
        directory separator or ``..`` component (it is the only value used to
        build the *local* write path). ``sub.path`` is preserved verbatim
        because it is used SOLELY to build the remote GET URL / SMB fetch path
        -- it must never be used to construct a local filesystem write path.
        Entries whose sanitized name is empty are dropped.
        """
        if isinstance(item, str):
            name = _safe_name(item)
            if not name:
                return None
            return SubInfo(name=name, path=item)
        if isinstance(item, dict):
            raw_name = None
            for key in _NAME_KEYS:
                if item.get(key):
                    raw_name = str(item[key])
                    break
            path = item.get("path")
            if path is None:
                path = raw_name
            if path is None:
                return None
            path = str(path)
            # Basename the untrusted name (dict branch matches the string
            # branch); fall back to the basename of ``path`` when absent.
            name = _safe_name(raw_name if raw_name is not None else path)
            if not name:
                return None
            size = item.get("size")
            return SubInfo(
                name=name,
                path=path,
                size=int(size) if isinstance(size, (int, float)) else None,
                target=item.get("target"),
            )
        return None

    def _parse_listing(self, raw: Any, target: str | None) -> list[SubInfo]:
        """Tolerantly parse a device listing into FITS-only :class:`SubInfo`s.

        Accepts a list of filename strings, a list of dicts (keys like
        ``name``/``filename``, ``path``, ``size``), or a dict wrapping such a
        list under ``files``/``list``/``result``. Unknown/empty shapes yield
        ``[]``. Non-FITS entries are filtered out. When ``target`` is not given,
        it is inferred from a ``<Target>_sub`` path segment.
        """
        subs: list[SubInfo] = []
        for item in self._coerce_to_list(raw):
            info = self._coerce_item(item)
            if info is None or not _is_fits(info.name):
                continue
            if info.target is None:
                info.target = target or _infer_target(info.path)
            subs.append(info)
        return subs

    # --- filesystem / UNC (SMB-mount) access -------------------------------

    @staticmethod
    def _find_sub_dir(image_root: str | Path, target: str) -> Path | None:
        """Return the best ``<Target>_sub``/``-sub`` dir under ``image_root``.

        Among the immediate subdirectories of ``image_root`` whose name ends
        with ``_sub`` or ``-sub``, match those whose base name (the part before
        the suffix) normalizes (see :func:`_normalize_target`) to ``target``.
        Prefer the ``_sub`` variant; when both variants match, pick the one with
        more ``.fit``/``.fits`` files. Returns ``None`` if none match or
        ``image_root`` is unreadable. Never raises.
        """
        want = _normalize_target(target)
        if not want:
            return None
        root = Path(image_root)
        try:
            entries = list(root.iterdir())
        except OSError:
            return None

        # (prefer_underscore, fit_count, dir) — higher sorts first.
        candidates: list[tuple[int, int, Path]] = []
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            lower = entry.name.lower()
            if lower.endswith("_sub"):
                prefer = 1
            elif lower.endswith("-sub"):
                prefer = 0
            else:
                continue
            base = entry.name[:-4]  # strip the 4-char "_sub"/"-sub" suffix
            if _normalize_target(base) != want:
                continue
            try:
                fit_count = sum(
                    1 for p in entry.iterdir() if p.suffix.lower() in _FITS_SUFFIXES
                )
            except OSError:
                fit_count = 0
            candidates.append((prefer, fit_count, entry))

        if not candidates:
            return None
        candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
        return candidates[0][2]

    def _list_subs_fs(self, target: str | None) -> list[SubInfo]:
        """List FITS subs for ``target`` directly from the filesystem.

        Resolves ``<image_root>/<Target>_sub`` via :meth:`_find_sub_dir`, globs
        ``*.fit``/``*.fits`` (the ``.jpg`` previews are excluded), and returns
        :class:`SubInfo`s with absolute filesystem paths, sorted by name. Empty
        list when no matching dir. Never raises.
        """
        if not target:
            # No target = "list everything", not "there is nothing". This used to
            # return [] here, so list_subs() with no argument answered
            # {"ok": true, "count": 0} — a caller asking what the scope holds was
            # told nothing, when the true answer was everything. The tool
            # documents `target` as optional, so honour that: walk each
            # `<Target>_sub` directory under the image root.
            out: list[SubInfo] = []
            try:
                entries = sorted(Path(self.image_root).iterdir())
            except OSError:
                return []
            for entry in entries:
                if not entry.is_dir():
                    continue
                match = _TARGET_SUB_RE.match(entry.name)
                if match is None:
                    continue
                out.extend(self._list_subs_fs(match.group("target")))
            return out
        sub_dir = self._find_sub_dir(self.image_root, target)
        if sub_dir is None:
            return []
        try:
            paths = list(sub_dir.glob("*.fit")) + list(sub_dir.glob("*.fits"))
        except OSError:
            return []
        subs: list[SubInfo] = []
        for path in sorted(paths, key=lambda p: p.name):
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            subs.append(
                SubInfo(name=path.name, path=str(path), size=size, target=target)
            )
        return subs

    # --- listing -----------------------------------------------------------

    async def list_subs(self, target: str | None = None) -> list[SubInfo]:
        """Enumerate saved FITS subs, optionally filtered to one ``target``."""
        # Filesystem/UNC path: when an image root is configured, read subs
        # directly off the mounted share (OS SMB redirector) — no JSON-RPC/HTTP.
        if self.image_root:
            subs = self._list_subs_fs(target)
            self._log(
                tool="data.list_subs",
                args={"target": target, "count": len(subs), "source": "fs"},
            )
            return subs
        # FIRMWARE-DEPENDENT: params *shape* for the listing method. Unconfirmed
        # against hardware; keep both the method name and this shape updatable.
        params: list = [{"target": target}] if target else []
        raw = await self._alpaca.method_sync(self.list_method, params)
        subs = self._parse_listing(raw, target)
        self._log(
            tool="data.list_subs",
            args={"target": target, "count": len(subs)},
        )
        return subs

    # --- downloading -------------------------------------------------------

    async def download_subs(
        self,
        subs: list[SubInfo],
        dest: str | Path | None = None,
        *,
        transport: str = "http",
    ) -> list[dict]:
        """Download ``subs`` to ``dest``; return per-sub result dicts.

        ``dest`` defaults to the settings ``data_dir`` when this client was
        built via :meth:`from_settings`; otherwise it is required. For each sub
        the chosen ``transport`` is attempted; on any HTTP failure (network
        error or non-2xx) with ``transport == "http"``, SMB is used
        automatically. Each downloaded file is hashed and provenance-logged.
        """
        dest_dir = Path(dest) if dest is not None else self._data_dir
        if dest_dir is None:
            raise ValueError(
                "dest is required (DataClient was not built via from_settings)"
            )
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_root = dest_dir.resolve()

        results: list[dict] = []
        for sub in subs:
            # Containment check (defense in depth): even though listing-derived
            # names are basenamed at the source, a SubInfo may be constructed
            # directly, so prove the resolved write path stays inside dest_dir
            # BEFORE touching the network. Fail closed + audit on any escape.
            # Windows path syntax is refused before resolve(): on Windows,
            # resolving a UNC name would itself reach out to that host.
            if _has_windows_path_syntax(sub.name):
                local_path = None
            else:
                local_path = (dest_dir / sub.name).resolve()
            if local_path is None or not local_path.is_relative_to(dest_root):
                self._log(
                    tool="data.download.rejected",
                    args={"name": sub.name},
                    note="path traversal blocked",
                )
                raise ValueError(
                    f"refusing to write outside data dir: {sub.name!r}"
                )

            # Filesystem/UNC source: when ``sub.path`` is an existing file (a
            # UNC/mapped/local path from ``_list_subs_fs``), copy it directly
            # over the OS SMB redirector — no HTTP/SMB fetch. Device-relative
            # URL paths won't exist as files and fall through to HTTP-then-SMB.
            source = Path(sub.path)
            try:
                is_local_file = source.is_file()
            except OSError:
                is_local_file = False
            if is_local_file:
                shutil.copy2(source, local_path)
                fits_hash = hash_fits(local_path)
                self._log(
                    tool="data.download",
                    args={"name": sub.name, "transport_used": "fs"},
                    fits_hash=fits_hash,
                )
                results.append(
                    {
                        "name": sub.name,
                        "path": str(local_path),
                        "transport": "fs",
                        "sha256": fits_hash,
                        "bytes": local_path.stat().st_size,
                    }
                )
                continue

            data, used, http_status = await self._fetch(sub, dest_dir, transport)
            local_path.write_bytes(data)
            fits_hash = hash_fits(data)
            self._log(
                tool="data.download",
                args={"name": sub.name, "transport_used": used},
                response_code=http_status,
                fits_hash=fits_hash,
            )
            results.append(
                {
                    "name": sub.name,
                    "path": str(local_path),
                    "transport": used,
                    "sha256": fits_hash,
                    "bytes": len(data),
                }
            )
        return results

    async def _fetch(
        self, sub: SubInfo, dest_dir: Path, transport: str
    ) -> tuple[bytes, str, int | None]:
        """Return ``(data, transport_used, http_status)`` for one sub."""
        if transport == "http":
            try:
                data, status = await self._download_http(sub)
                return data, "http", status
            except (httpx.HTTPError, httpx.HTTPStatusError):
                # HTTP failed (network error or non-2xx) -> automatic SMB fallback.
                data = await self._download_smb(sub, dest_dir)
                return data, "smb", None
        # Explicit non-HTTP transport (e.g. "smb"): no HTTP attempt at all.
        data = await self._download_smb(sub, dest_dir)
        return data, "smb", None

    async def _download_http(self, sub: SubInfo) -> tuple[bytes, int]:
        """GET the sub from the device HTTP server; raise on network/non-2xx."""
        # URL-encode the path but preserve directory separators.
        url = f"http://{self._host}:{self._http_port}/{quote(sub.path, safe='/')}"
        response = await self._http.get(url)
        response.raise_for_status()
        return response.content, response.status_code

    async def _download_smb(self, sub: SubInfo, dest: Path) -> bytes:
        """Read the sub over SMB and return its bytes (never a real conn in tests).

        Kept deliberately small and mockable: tests monkeypatch this method or
        ``smbclient.open_file``. No SMB server is contacted during tests.
        """
        import smbclient

        # SMB UNC paths use backslashes; sub.path is stored with forward slashes.
        unc_path = sub.path.replace("/", "\\")
        # FIRMWARE-DEPENDENT: share name (see module docstring / DEFAULT_SMB_SHARE).
        target = rf"\\{self._host}\{self._smb_share}\{unc_path}"
        with smbclient.open_file(target, mode="rb") as handle:
            return handle.read()
