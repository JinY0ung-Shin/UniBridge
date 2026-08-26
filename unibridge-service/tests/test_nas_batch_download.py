"""Tests for the NAS batch ZIP download endpoint (POST /nas/{alias}/download-zip).

Uses a REAL temp directory tree registered on the live NASConnectionManager
singleton (not mocks), so the whole path goes through the actual security
kernel, the nas-fs executor and the streaming zipfile writer.
"""
from __future__ import annotations

import io
import os
import zipfile
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.models import NASConnection
from app.services.nas_manager import nas_manager
from tests.conftest import auth_header

A_TXT = b"alpha payload"
B_BIN = b"\x00\x01\x02" * 1000


async def _register(
    alias: str,
    base_path,
    *,
    max_download_bytes: int | None = None,
    show_hidden: bool = False,
    follow_symlinks: bool = False,
) -> None:
    await nas_manager.add_connection(
        NASConnection(
            alias=alias,
            base_path=str(base_path),
            read_only=True,
            max_download_bytes=max_download_bytes,
            show_hidden=show_hidden,
            follow_symlinks=follow_symlinks,
        )
    )


@pytest.fixture
def nas_root(tmp_path, monkeypatch):
    """Isolate the manager singleton and allow tmp_path as a NAS root."""
    saved_bases = dict(nas_manager._bases)
    saved_configs = dict(nas_manager._configs)
    nas_manager._bases = {}
    nas_manager._configs = {}
    monkeypatch.setattr(settings, "NAS_ALLOWED_ROOTS", os.path.realpath(str(tmp_path)))
    yield tmp_path
    nas_manager._bases = saved_bases
    nas_manager._configs = saved_configs


@pytest.fixture
async def share(nas_root):
    """A populated share registered as alias ``nas`` (hidden + symlinks off)."""
    base = nas_root / "share"
    (base / "sub").mkdir(parents=True)
    (base / "a.txt").write_bytes(A_TXT)
    (base / "sub" / "b.bin").write_bytes(B_BIN)
    (base / ".hidden").write_bytes(b"secret")
    (base / "adir").mkdir()
    os.symlink(base / "a.txt", base / "link.txt")
    os.mkfifo(base / "pipe")
    await _register("nas", base)
    return base


async def _create_apikey(client, admin_token, *, name, key, allowed_databases, allowed_routes):
    with patch("app.routers.api_keys.apisix_client") as mock_apisix:
        mock_apisix.put_resource = AsyncMock(return_value={
            "username": name,
            "plugins": {"key-auth": {"key": key}},
        })
        mock_apisix.get_resource = AsyncMock(side_effect=Exception("not found"))
        mock_apisix.list_resources = AsyncMock(return_value={"items": []})

        resp = await client.post(
            "/admin/api-keys",
            json={
                "name": name,
                "api_key": key,
                "allowed_databases": allowed_databases,
                "allowed_routes": allowed_routes,
            },
            headers=auth_header(admin_token),
        )
    assert resp.status_code == 201, resp.text


async def _post_zip(client, token, alias, paths):
    return await client.post(
        f"/nas/{alias}/download-zip",
        json={"paths": paths},
        headers=auth_header(token),
    )


# ── happy path / archive contract ────────────────────────────────────────────


async def test_zip_two_files_preserves_subdirectory_structure(client, admin_token, share):
    resp = await _post_zip(client, admin_token, "nas", ["a.txt", "sub/b.bin"])

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/zip")
    assert resp.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''nas-files.zip"
    )
    assert resp.headers["x-content-type-options"] == "nosniff"
    # Built while streaming, so the final length is not knowable up front.
    assert "content-length" not in resp.headers

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert zf.namelist() == ["a.txt", "sub/b.bin"]
    assert zf.read("a.txt") == A_TXT
    assert zf.read("sub/b.bin") == B_BIN
    assert zf.testzip() is None


async def test_zip_survives_a_chunk_size_smaller_than_the_files(
    client, admin_token, share, monkeypatch
):
    """Multi-chunk members must produce a valid archive, not just single-read ones."""
    monkeypatch.setattr(settings, "NAS_STREAM_CHUNK_BYTES", 7)

    resp = await _post_zip(client, admin_token, "nas", ["a.txt", "sub/b.bin"])

    assert resp.status_code == 200, resp.text
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert zf.read("sub/b.bin") == B_BIN
    assert zf.testzip() is None


async def test_zip_deduplicates_repeated_path(client, admin_token, share):
    resp = await _post_zip(client, admin_token, "nas", ["a.txt", "a.txt"])

    assert resp.status_code == 200, resp.text
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert zf.namelist() == ["a.txt"]
    assert zf.read("a.txt") == A_TXT


async def test_zip_is_streamed_with_data_descriptors(share, monkeypatch):
    """The archive must be emitted incrementally, never buffered whole.

    Asserted at the manager so chunk boundaries are visible: many yields for a
    file far larger than the chunk size, and flag bit 3 set on the local header
    (zipfile only sets it when its output is unseekable, i.e. our sink).
    """
    monkeypatch.setattr(settings, "NAS_STREAM_CHUNK_BYTES", 256)

    gen, meta = await nas_manager.open_zip_stream("nas", ["a.txt", "sub/b.bin"])
    chunks = [chunk async for chunk in gen]

    assert meta == {
        "filename": "nas-files.zip",
        "content_type": "application/zip",
        "file_count": 2,
        "total_size": len(A_TXT) + len(B_BIN),
    }
    assert len(chunks) > len(B_BIN) // 256
    blob = b"".join(chunks)
    assert blob[:4] == b"PK\x03\x04"
    assert int.from_bytes(blob[6:8], "little") & 0x08, "local header lacks the data-descriptor bit"
    assert blob.count(b"PK\x07\x08") >= 2, "expected one data descriptor per member"
    assert zipfile.ZipFile(io.BytesIO(blob)).testzip() is None


async def test_zip_clamps_pre_1980_mtime(client, admin_token, share):
    """ZIP timestamps cannot predate 1980; an epoch mtime must clamp, not 500."""
    os.utime(share / "a.txt", (0, 0))

    resp = await _post_zip(client, admin_token, "nas", ["a.txt"])

    assert resp.status_code == 200, resp.text
    info = zipfile.ZipFile(io.BytesIO(resp.content)).getinfo("a.txt")
    assert info.date_time == (1980, 1, 1, 0, 0, 0)


# ── per-path failures (nothing streamed) ─────────────────────────────────────


async def test_zip_missing_file_404_names_the_path(client, admin_token, share):
    resp = await _post_zip(client, admin_token, "nas", ["a.txt", "sub/missing.txt"])

    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "sub/missing.txt" in detail
    assert str(share) not in detail


async def test_zip_traversal_400(client, admin_token, share):
    resp = await _post_zip(client, admin_token, "nas", ["../evil"])

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "../evil" in detail
    assert str(share) not in detail


async def test_zip_hidden_file_404_when_not_shown(client, admin_token, share):
    resp = await _post_zip(client, admin_token, "nas", [".hidden"])

    assert resp.status_code == 404
    assert ".hidden" in resp.json()["detail"]


async def test_zip_directory_target_400(client, admin_token, share):
    resp = await _post_zip(client, admin_token, "nas", ["adir"])

    assert resp.status_code == 400
    assert "adir" in resp.json()["detail"]


async def test_zip_symlink_400_when_not_followed(client, admin_token, share):
    resp = await _post_zip(client, admin_token, "nas", ["link.txt"])

    assert resp.status_code == 400
    assert "link.txt" in resp.json()["detail"]


async def test_zip_fifo_400(client, admin_token, share):
    """A FIFO is rejected as a non-regular file, and must not block on the open."""
    resp = await _post_zip(client, admin_token, "nas", ["pipe"])

    assert resp.status_code == 400
    assert "pipe" in resp.json()["detail"]


async def test_zip_total_size_over_cap_413(client, admin_token, nas_root):
    base = nas_root / "capped"
    (base / "sub").mkdir(parents=True)
    (base / "a.txt").write_bytes(A_TXT)
    (base / "sub" / "b.bin").write_bytes(B_BIN)
    await _register("tiny", base, max_download_bytes=20)

    resp = await _post_zip(client, admin_token, "tiny", ["a.txt", "sub/b.bin"])

    assert resp.status_code == 413
    assert "sub/b.bin" in resp.json()["detail"]


# ── request-level validation ─────────────────────────────────────────────────


async def test_zip_too_many_paths_400(client, admin_token, share, monkeypatch):
    monkeypatch.setattr(settings, "NAS_MAX_BATCH_FILES", 1)

    resp = await _post_zip(client, admin_token, "nas", ["a.txt", "sub/b.bin"])

    assert resp.status_code == 400
    assert "max 1" in resp.json()["detail"]


async def test_zip_empty_paths_422(client, admin_token, share):
    resp = await _post_zip(client, admin_token, "nas", [])

    assert resp.status_code == 422


async def test_zip_unknown_alias_404(client, admin_token, share):
    resp = await _post_zip(client, admin_token, "nope", ["a.txt"])

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


# ── authorization ────────────────────────────────────────────────────────────


async def test_zip_jwt_without_nas_browse_403(client, user_token, share):
    """The seeded ``user`` role does NOT carry nas.browse."""
    resp = await _post_zip(client, user_token, "nas", ["a.txt"])

    assert resp.status_code == 403


async def test_zip_apikey_rejects_unallowed_alias(client, admin_token, share):
    await _create_apikey(
        client,
        admin_token,
        name="zip-other",
        key="zip-other-key",
        allowed_databases=["some-other-nas"],
        allowed_routes=["nas-api"],
    )

    resp = await client.post(
        "/nas/nas/download-zip",
        json={"paths": ["a.txt"]},
        headers={"X-Consumer-Username": "zip-other"},
    )

    assert resp.status_code == 403
    assert "not allowed" in resp.json()["detail"].lower()


async def test_zip_apikey_allows_configured_alias(client, admin_token, share):
    await _create_apikey(
        client,
        admin_token,
        name="zip-app",
        key="zip-app-key",
        allowed_databases=["nas"],
        allowed_routes=["nas-api"],
    )

    resp = await client.post(
        "/nas/nas/download-zip",
        json={"paths": ["a.txt"]},
        headers={"X-Consumer-Username": "zip-app"},
    )

    assert resp.status_code == 200, resp.text
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert zf.read("a.txt") == A_TXT


# ── descriptor hygiene ───────────────────────────────────────────────────────


def _open_fd_count() -> int | None:
    if not os.path.isdir("/proc/self/fd"):
        return None
    return len(os.listdir("/proc/self/fd"))


async def test_zip_does_not_leak_descriptors(client, admin_token, nas_root):
    """Neither a completed archive nor a pre-stream 413 may strand an fd."""
    base = nas_root / "fds"
    base.mkdir()
    names = [f"f{i}.txt" for i in range(6)]
    for name in names:
        (base / name).write_bytes(b"x" * 64)
    await _register("many", base)
    await _register("many-capped", base, max_download_bytes=100)

    # Warm up the nas-fs executor threads so their fds are not counted as leaks.
    await _post_zip(client, admin_token, "many", names)

    before = _open_fd_count()
    if before is None:
        pytest.skip("no /proc/self/fd on this platform")

    ok = await _post_zip(client, admin_token, "many", names)
    assert ok.status_code == 200, ok.text

    too_big = await _post_zip(client, admin_token, "many-capped", names)
    assert too_big.status_code == 413

    assert _open_fd_count() == before
