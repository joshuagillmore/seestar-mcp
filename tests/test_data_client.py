"""Tests for seestar_mcp.data_client.

Uses respx to mock the device's built-in HTTP file server (:80) and a mocked
AlpacaClient (``unittest.mock.AsyncMock``) whose ``method_sync`` returns canned
listing data. SMB is never actually contacted: the ``_download_smb`` helper (or
``smbclient.open_file``) is monkeypatched. Because pytest is configured with
``asyncio_mode=auto``, async tests need no decorator.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from seestar_mcp.data_client import DataClient, SubInfo
from seestar_mcp.provenance import ProvenanceLog, hash_fits

HOST = "testscope"
FITS_BYTES = b"SIMPLE  =                    T / fake fits\x00padding-bytes-here"


def _make_client(**kwargs):
    """Build a DataClient with an injected httpx client + AsyncMock alpaca."""
    alpaca = AsyncMock()
    http = httpx.AsyncClient(timeout=5.0)
    client = DataClient(alpaca, host=HOST, http_client=http, **kwargs)
    return client, alpaca, http


# --- _parse_listing -------------------------------------------------------


def test_parse_listing_list_of_strings():
    client, _, _ = _make_client()
    raw = [
        "M31_sub/DSO_Stacked_1_M31_10s_20260704_2231.fit",
        "M31_sub/preview.jpg",  # non-FITS, filtered out
        "M31_sub/DSO_Stacked_2_M31_10s_20260704_2232.fits",
    ]
    subs = client._parse_listing(raw, None)
    assert [s.name for s in subs] == [
        "DSO_Stacked_1_M31_10s_20260704_2231.fit",
        "DSO_Stacked_2_M31_10s_20260704_2232.fits",
    ]
    # target inferred from the "<Target>_sub" path segment.
    assert all(s.target == "M31" for s in subs)
    assert subs[0].path == "M31_sub/DSO_Stacked_1_M31_10s_20260704_2231.fit"


def test_parse_listing_list_of_dicts():
    client, _, _ = _make_client()
    raw = [
        {
            "name": "DSO_Stacked_1_M31_10s.fit",
            "path": "M31_sub/DSO_Stacked_1_M31_10s.fit",
            "size": 12_582_912,
        },
        {"filename": "notes.txt", "path": "M31_sub/notes.txt"},  # non-FITS
    ]
    subs = client._parse_listing(raw, None)
    assert len(subs) == 1
    assert subs[0].name == "DSO_Stacked_1_M31_10s.fit"
    assert subs[0].size == 12_582_912
    assert subs[0].target == "M31"


def test_parse_listing_dict_wrapping_a_list():
    client, _, _ = _make_client()
    for key in ("files", "list", "result"):
        raw = {key: ["Tgt_sub/DSO_Stacked_1_Tgt_10s.fit"]}
        subs = client._parse_listing(raw, None)
        assert len(subs) == 1
        assert subs[0].name == "DSO_Stacked_1_Tgt_10s.fit"
        assert subs[0].target == "Tgt"


def test_parse_listing_explicit_target_wins_over_inference():
    client, _, _ = _make_client()
    subs = client._parse_listing(["bare_file.fit"], "Andromeda")
    assert len(subs) == 1
    assert subs[0].target == "Andromeda"


def test_parse_listing_garbage_returns_empty():
    client, _, _ = _make_client()
    assert client._parse_listing(None, None) == []
    assert client._parse_listing({}, None) == []
    assert client._parse_listing(12345, None) == []
    assert client._parse_listing({"unknown": [1, 2, 3]}, None) == []


# --- list_subs ------------------------------------------------------------


async def test_list_subs_calls_method_sync_once_and_parses(tmp_path):
    prov = ProvenanceLog(tmp_path / "prov.jsonl")
    client, alpaca, _ = _make_client(provenance=prov, list_method="get_img_file_list")
    alpaca.method_sync.return_value = [
        "M31_sub/DSO_Stacked_1_M31_10s.fit",
        "M31_sub/DSO_Stacked_2_M31_10s.fit",
    ]
    subs = await client.list_subs(target="M31")

    alpaca.method_sync.assert_awaited_once()
    call = alpaca.method_sync.await_args
    assert call.args[0] == "get_img_file_list"
    # params shape: [{"target": target}] when a target is given.
    assert call.args[1] == [{"target": "M31"}]
    assert len(subs) == 2

    lines = (tmp_path / "prov.jsonl").read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])
    assert record["tool"] == "data.list_subs"
    assert record["args"] == {"target": "M31", "count": 2}


async def test_list_subs_no_target_sends_empty_params():
    client, alpaca, _ = _make_client()
    alpaca.method_sync.return_value = []
    await client.list_subs()
    call = alpaca.method_sync.await_args
    assert call.args[1] == []


async def test_list_subs_empty_or_garbage_returns_empty():
    client, alpaca, _ = _make_client()
    alpaca.method_sync.return_value = {"weird": "shape"}
    subs = await client.list_subs()
    assert subs == []


# --- download_subs: HTTP --------------------------------------------------


@respx.mock
async def test_download_subs_http_success(tmp_path):
    prov = ProvenanceLog(tmp_path / "prov.jsonl")
    client, _, _ = _make_client(provenance=prov)
    sub = SubInfo(
        name="DSO_Stacked_1_M31_10s.fit",
        path="M31_sub/DSO_Stacked_1_M31_10s.fit",
        target="M31",
    )
    route = respx.get(
        f"http://{HOST}:80/M31_sub/DSO_Stacked_1_M31_10s.fit"
    ).mock(return_value=httpx.Response(200, content=FITS_BYTES))

    results = await client.download_subs([sub], dest=tmp_path)
    assert route.called
    assert len(results) == 1
    r = results[0]
    assert r["transport"] == "http"
    assert r["sha256"] == hash_fits(FITS_BYTES)
    assert r["bytes"] == len(FITS_BYTES)

    local = tmp_path / "DSO_Stacked_1_M31_10s.fit"
    assert local.read_bytes() == FITS_BYTES
    assert r["path"] == str(local)

    lines = (tmp_path / "prov.jsonl").read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])
    assert record["tool"] == "data.download"
    assert record["fits_hash"] == hash_fits(FITS_BYTES)
    assert record["args"]["transport_used"] == "http"


@respx.mock
async def test_download_subs_http_failure_falls_back_to_smb(tmp_path, monkeypatch):
    client, _, _ = _make_client()
    sub = SubInfo(name="x.fit", path="M31_sub/x.fit", target="M31")
    respx.get(f"http://{HOST}:80/M31_sub/x.fit").mock(
        return_value=httpx.Response(500)
    )

    async def fake_smb(self, sub_arg, dest):
        return FITS_BYTES

    monkeypatch.setattr(DataClient, "_download_smb", fake_smb)

    results = await client.download_subs([sub], dest=tmp_path)
    assert results[0]["transport"] == "smb"
    assert results[0]["sha256"] == hash_fits(FITS_BYTES)
    assert (tmp_path / "x.fit").read_bytes() == FITS_BYTES


@respx.mock
async def test_download_subs_http_network_error_falls_back(tmp_path, monkeypatch):
    client, _, _ = _make_client()
    sub = SubInfo(name="x.fit", path="M31_sub/x.fit", target="M31")
    respx.get(f"http://{HOST}:80/M31_sub/x.fit").mock(
        side_effect=httpx.ConnectError("boom")
    )

    async def fake_smb(self, sub_arg, dest):
        return FITS_BYTES

    monkeypatch.setattr(DataClient, "_download_smb", fake_smb)
    results = await client.download_subs([sub], dest=tmp_path)
    assert results[0]["transport"] == "smb"


async def test_download_subs_explicit_smb_bypasses_http(tmp_path, monkeypatch):
    client, _, _ = _make_client()
    sub = SubInfo(name="x.fit", path="M31_sub/x.fit", target="M31")

    called = {"smb": False}

    async def fake_smb(self, sub_arg, dest):
        called["smb"] = True
        return FITS_BYTES

    monkeypatch.setattr(DataClient, "_download_smb", fake_smb)
    # No respx mock at all: if HTTP were attempted it would raise.
    results = await client.download_subs([sub], dest=tmp_path, transport="smb")
    assert called["smb"] is True
    assert results[0]["transport"] == "smb"
    assert (tmp_path / "x.fit").read_bytes() == FITS_BYTES


async def test_download_smb_helper_uses_smbclient_open_file(tmp_path, monkeypatch):
    """Exercise the real _download_smb body with a fake smbclient.open_file."""
    import smbclient

    client, _, _ = _make_client()
    sub = SubInfo(name="x.fit", path="M31_sub/x.fit", target="M31")

    captured = {}

    class _FakeFile:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return FITS_BYTES

    def fake_open_file(path, mode="rb", **kwargs):
        captured["path"] = path
        captured["mode"] = mode
        return _FakeFile()

    monkeypatch.setattr(smbclient, "open_file", fake_open_file)
    data = await client._download_smb(sub, tmp_path)
    assert data == FITS_BYTES
    # UNC path uses backslashes and the (firmware-dependent) share name.
    assert captured["path"].startswith(rf"\\{HOST}")
    assert "x.fit" in captured["path"]


# --- lifecycle ------------------------------------------------------------


async def test_aclose_closes_owned_but_not_injected_client():
    # Injected client: must NOT be closed by DataClient.aclose().
    injected = httpx.AsyncClient()
    dc_injected = DataClient(AsyncMock(), host=HOST, http_client=injected)
    await dc_injected.aclose()
    assert injected.is_closed is False
    await injected.aclose()

    # Owned client: aclose() closes it.
    dc_owned = DataClient(AsyncMock(), host=HOST)
    owned = dc_owned._http
    await dc_owned.aclose()
    assert owned.is_closed is True


async def test_context_manager_closes_owned_client():
    async with DataClient(AsyncMock(), host=HOST) as dc:
        owned = dc._http
        assert owned.is_closed is False
    assert owned.is_closed is True


async def test_from_settings_defaults_dest_to_data_dir(tmp_path, monkeypatch):
    from seestar_mcp.config import Settings

    settings = Settings(seestar_host=HOST, data_dir=tmp_path / "data")
    alpaca = AsyncMock()
    dc = DataClient.from_settings(settings, alpaca)
    sub = SubInfo(name="x.fit", path="M31_sub/x.fit", target="M31")

    async def fake_smb(self, sub_arg, dest):
        return FITS_BYTES

    monkeypatch.setattr(DataClient, "_download_smb", fake_smb)
    results = await dc.download_subs([sub], transport="smb")  # dest omitted
    assert results[0]["path"] == str(tmp_path / "data" / "x.fit")
    await dc.aclose()


async def test_download_requires_dest_without_settings(tmp_path):
    client, _, _ = _make_client()
    sub = SubInfo(name="x.fit", path="M31_sub/x.fit", target="M31")
    with pytest.raises(ValueError):
        await client.download_subs([sub])  # no dest, not from_settings


# --- security: path traversal in untrusted device listings ----------------


def test_coerce_item_dict_name_is_basenamed():
    """A dict listing entry's name is reduced to a bare basename (untrusted)."""
    client, _, _ = _make_client()
    info = client._coerce_item(
        {"name": "..\\..\\..\\evil.fits", "path": "M31_sub/real.fits"}
    )
    assert info is not None
    # name must never carry a separator or traversal component.
    assert info.name == "evil.fits"
    assert "/" not in info.name and "\\" not in info.name
    # path is preserved verbatim: it is only used to build the remote fetch URL.
    assert info.path == "M31_sub/real.fits"


def test_list_subs_parse_basenames_malicious_dict_name():
    """End-to-end parse: malicious dict name yields a bare basename SubInfo."""
    client, _, _ = _make_client()
    subs = client._parse_listing(
        [{"name": "..\\..\\..\\evil.fits", "path": "M31_sub/real.fits"}], None
    )
    assert len(subs) == 1
    assert subs[0].name == "evil.fits"


@respx.mock
async def test_download_subs_source_sanitized_name_lands_inside(tmp_path):
    """A name sanitized at the source lands safely inside the dest dir."""
    client, _, _ = _make_client()
    sub = client._coerce_item(
        {"name": "..\\..\\..\\evil.fits", "path": "M31_sub/real.fits"}
    )
    assert sub is not None and sub.name == "evil.fits"
    route = respx.get(f"http://{HOST}:80/M31_sub/real.fits").mock(
        return_value=httpx.Response(200, content=FITS_BYTES)
    )

    results = await client.download_subs([sub], dest=tmp_path)
    assert route.called
    local = tmp_path / "evil.fits"
    assert local.read_bytes() == FITS_BYTES
    assert results[0]["path"] == str(local)
    # Nothing escaped the dest dir.
    assert not (tmp_path.parent / "evil.fits").exists()


async def test_download_subs_absolute_name_rejected_no_write(tmp_path):
    """A directly-constructed ABSOLUTE name fails closed (ValueError, no write)."""
    prov = ProvenanceLog(tmp_path / "prov.jsonl")
    client, _, _ = _make_client(provenance=prov)
    outside = tmp_path.parent / "escape.fits"
    if outside.exists():
        outside.unlink()
    # Absolute name: pathlib's `dest_dir / abs` discards dest_dir entirely.
    sub = SubInfo(name=str(outside), path="M31_sub/real.fits")

    with pytest.raises(ValueError):
        await client.download_subs([sub], dest=tmp_path)  # HTTP never attempted

    # Fail closed: no file written at the attacker-chosen outside location.
    assert not outside.exists()

    # The blocked attempt is audited.
    lines = (tmp_path / "prov.jsonl").read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])
    assert record["tool"] == "data.download.rejected"
    assert record["args"]["name"] == str(outside)


@pytest.mark.parametrize(
    "bad",
    [
        "C:\\Windows\\evil.fits",  # drive-absolute
        "C:evil.fits",  # drive-relative: lands inside dest on Windows, still refused
        "..\\evil.fits",  # backslash traversal
        "M31_sub\\evil.fits",  # backslash subdirectory
        "/etc/evil.fits",  # POSIX absolute
    ],
)
async def test_download_subs_windows_absolute_name_rejected(tmp_path, bad):
    """A drive-letter, backslash or absolute name is refused by the guard ITSELF.

    This used to pass on Linux CI for the wrong reason (2026-09-22 review): on
    POSIX ``C:\\Windows\\evil.fits`` is one legal filename, so the guard let it
    through, HTTP failed, and smbprotocol's own ``ValueError("Failed to
    connect…")`` satisfied a bare ``pytest.raises(ValueError)``. Now the message
    and the audit record must be the guard's, and any fetch fails the test.
    """
    prov = ProvenanceLog(tmp_path / "prov.jsonl")
    client, _, _ = _make_client(provenance=prov)
    client._fetch = AsyncMock(
        side_effect=AssertionError("guard must fire before any fetch")
    )
    sub = SubInfo(name=bad, path="M31_sub/real.fits")

    with pytest.raises(ValueError, match="refusing to write outside data dir"):
        await client.download_subs([sub], dest=tmp_path)

    lines = (tmp_path / "prov.jsonl").read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])
    assert record["tool"] == "data.download.rejected"
    assert record["args"]["name"] == bad


@pytest.mark.parametrize(
    ("name", "foreign"),
    [
        ("C:\\Windows\\evil.fits", True),
        ("C:evil.fits", True),
        ("c:/evil.fits", True),
        ("..\\evil.fits", True),
        ("\\\\host\\share\\evil.fits", True),
        ("Light_M 31_10.0s_IRCUT_20240101-223100.fit", False),
        ("DSO_Stacked_1_M31_10s_20260704_2231.fit", False),
    ],
)
def test_windows_path_syntax_is_detected_on_every_platform(name, foreign):
    """Pure string check, so it answers the same on Windows and Linux."""
    from seestar_mcp.data_client import _has_windows_path_syntax

    assert _has_windows_path_syntax(name) is foreign


# --- filesystem / UNC (SMB-mount) sub access ------------------------------


def _make_sub_dir(myworks: Path, dir_name: str, fit_names, jpg_names=()):
    """Create <myworks>/<dir_name> with the given .fit and .jpg files."""
    sub_dir = myworks / dir_name
    sub_dir.mkdir(parents=True, exist_ok=True)
    for name in fit_names:
        (sub_dir / name).write_bytes(FITS_BYTES)
    for name in jpg_names:
        (sub_dir / name).write_bytes(b"jpg-bytes")
    return sub_dir


def test_find_sub_dir_matches_spaced_name(tmp_path):
    """A spaced target dir ("M 31_sub") matches "M31"/"M 31"/"m31"; misses -> None."""
    myworks = tmp_path / "MyWorks"
    sub_dir = _make_sub_dir(
        myworks,
        "M 31_sub",
        ["Light_M 31_10.0s_IRCUT_a.fit",
         "Light_M 31_10.0s_IRCUT_b.fit",
         "Light_M 31_10.0s_LP_c.fit"],
        jpg_names=["Light_M 31_10.0s_IRCUT_a.jpg"],
    )
    assert DataClient._find_sub_dir(myworks, "M31") == sub_dir
    assert DataClient._find_sub_dir(myworks, "M 31") == sub_dir
    assert DataClient._find_sub_dir(myworks, "m31") == sub_dir
    assert DataClient._find_sub_dir(myworks, "NGC281") is None


def test_find_sub_dir_prefers_underscore_and_more_files(tmp_path):
    """Given both "M 31_sub" and "M 31-sub", the "_sub" variant is chosen."""
    myworks = tmp_path / "MyWorks"
    underscore = _make_sub_dir(myworks, "M 31_sub", ["a.fit", "b.fit", "c.fit"])
    _make_sub_dir(myworks, "M 31-sub", ["z.fit"])
    assert DataClient._find_sub_dir(myworks, "M31") == underscore


async def test_list_subs_fs(tmp_path):
    """image_root set -> list_subs reads .fit from the fs and skips method_sync + jpgs."""
    myworks = tmp_path / "MyWorks"
    fit_names = [
        "Light_M 31_10.0s_IRCUT_20260704_2231.fit",
        "Light_M 31_10.0s_IRCUT_20260704_2232.fit",
        "Light_M 31_10.0s_LP_20260704_2233.fit",
    ]
    _make_sub_dir(
        myworks,
        "M 31_sub",
        fit_names,
        jpg_names=[
            "Light_M 31_10.0s_IRCUT_20260704_2231.jpg",
            "Light_M 31_10.0s_IRCUT_20260704_2231_thn.jpg",
        ],
    )
    client, alpaca, _ = _make_client(image_root=str(myworks))
    subs = await client.list_subs("M31")

    assert len(subs) == 3
    assert [s.name for s in subs] == sorted(fit_names)
    assert all(s.path.endswith(".fit") for s in subs)
    assert all(Path(s.path).is_absolute() for s in subs)
    assert all(s.target == "M31" for s in subs)
    alpaca.method_sync.assert_not_awaited()


async def test_download_subs_fs_copies(tmp_path):
    """Subs with existing fs paths are copied (transport=="fs") + hashed."""
    myworks = tmp_path / "MyWorks"
    _make_sub_dir(myworks, "M 31_sub", ["a.fit", "b.fit"])
    client, _, _ = _make_client(image_root=str(myworks))
    subs = client._list_subs_fs("M31")
    assert len(subs) == 2

    out = tmp_path / "out"
    results = await client.download_subs(subs, dest=out)

    assert len(results) == 2
    for r in results:
        assert r["transport"] == "fs"
        assert r["sha256"] == hash_fits(FITS_BYTES)
        assert Path(r["path"]).exists()
    assert (out / "a.fit").read_bytes() == FITS_BYTES
    assert (out / "b.fit").read_bytes() == FITS_BYTES


async def test_image_root_empty_uses_method_sync():
    """Regression: image_root empty -> list_subs still uses alpaca.method_sync."""
    client, alpaca, _ = _make_client()  # image_root default ""
    alpaca.method_sync.return_value = ["M31_sub/DSO_Stacked_1_M31_10s.fit"]
    subs = await client.list_subs("M31")
    alpaca.method_sync.assert_awaited_once()
    assert len(subs) == 1


# --- list_subs with no target must enumerate, not report "none" -------------
# Found by exercising every tool surface (2026-08-03). list_subs(target=None)
# returned {"ok": true, "count": 0, "subs": []} because _list_subs_fs bailed on
# `if not target: return []`. The tool documents target as OPTIONAL, so a caller
# asking "what's on the scope?" was told "nothing" when the real answer was
# "everything". Contract rule 4: unknown/unasked is not empty.


def test_list_subs_without_a_target_enumerates_every_target(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock

    from seestar_mcp.data_client import DataClient

    for name, n in (("M13", 3), ("NGC7635", 2)):
        d = tmp_path / f"{name}_sub"
        d.mkdir()
        for i in range(n):
            (d / f"Light_{name}_10.0s_{i}.fit").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")          # ignored
    (tmp_path / "M13").mkdir()                          # stacked dir, not _sub

    dc = DataClient(alpaca=AsyncMock(), host="127.0.0.1", image_root=str(tmp_path))
    subs = asyncio.run(dc.list_subs(None))

    assert len(subs) == 5, f"expected 5 subs across both targets, got {len(subs)}"
    assert {s.target for s in subs} == {"M13", "NGC7635"}


def test_list_subs_with_a_target_still_filters(tmp_path):
    """REGRESSION: the per-target path must keep working unchanged."""
    import asyncio
    from unittest.mock import AsyncMock

    from seestar_mcp.data_client import DataClient

    for name, n in (("M13", 3), ("NGC7635", 2)):
        d = tmp_path / f"{name}_sub"
        d.mkdir()
        for i in range(n):
            (d / f"Light_{name}_10.0s_{i}.fit").write_bytes(b"x")

    dc = DataClient(alpaca=AsyncMock(), host="127.0.0.1", image_root=str(tmp_path))
    subs = asyncio.run(dc.list_subs("M13"))
    assert len(subs) == 3
    assert {s.target for s in subs} == {"M13"}
