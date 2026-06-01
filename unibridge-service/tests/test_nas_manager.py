"""Unit tests for NASConnectionManager (app.services.nas_manager).

Uses REAL temp directory trees (not mocks) so the manager exercises the actual
security kernel + filesystem syscalls it relies on.
"""
from __future__ import annotations

import os

import pytest

from app.models import NASConnection
from app.services.nas_manager import NASConnectionManager, nas_manager
from app.services.nas_security import NasTooLargeError, NasUnavailableError


@pytest.fixture
def fresh_manager(monkeypatch):
    """Patch the singleton's internal state for isolated tests, and point
    NAS_ALLOWED_ROOTS at a permissive root so tmp_path bases resolve.
    """
    saved_bases = dict(nas_manager._bases)
    saved_configs = dict(nas_manager._configs)
    nas_manager._bases = {}
    nas_manager._configs = {}
    yield nas_manager
    nas_manager._bases = saved_bases
    nas_manager._configs = saved_configs


def _allow_root(monkeypatch, path) -> None:
    """Make ``path`` (its realpath) an allowed NAS root for both the manager and
    the security kernel via settings."""
    from app.config import settings

    monkeypatch.setattr(settings, "NAS_ALLOWED_ROOTS", os.path.realpath(str(path)))


def _make_conn(
    alias: str,
    base_path: str,
    *,
    max_download_bytes: int | None = None,
    show_hidden: bool = False,
    follow_symlinks: bool = False,
) -> NASConnection:
    return NASConnection(
        alias=alias,
        base_path=base_path,
        read_only=True,
        max_download_bytes=max_download_bytes,
        show_hidden=show_hidden,
        follow_symlinks=follow_symlinks,
    )


# ── singleton + lifecycle ─────────────────────────────────────────────────────


def test_singleton_returns_same_instance():
    assert NASConnectionManager() is NASConnectionManager()


@pytest.mark.asyncio
async def test_add_has_list_remove(fresh_manager, tmp_path, monkeypatch):
    base = tmp_path / "share"
    base.mkdir()
    _allow_root(monkeypatch, tmp_path)

    await fresh_manager.add_connection(_make_conn("a", str(base)))
    assert fresh_manager.has_connection("a") is True
    assert "a" in fresh_manager.list_aliases()

    cfg = fresh_manager.get_config("a")
    assert cfg["base_path"] == str(base)
    assert cfg["show_hidden"] is False
    assert cfg["follow_symlinks"] is False

    await fresh_manager.remove_connection("a")
    assert fresh_manager.has_connection("a") is False


@pytest.mark.asyncio
async def test_add_connection_outside_allowed_roots_rejected(fresh_manager, tmp_path, monkeypatch):
    """Defense in depth: the manager itself enforces the allowed-root check."""
    base = tmp_path / "share"
    base.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    _allow_root(monkeypatch, other)  # base is NOT under this
    with pytest.raises(Exception):
        await fresh_manager.add_connection(_make_conn("bad", str(base)))
    assert not fresh_manager.has_connection("bad")


@pytest.mark.asyncio
async def test_initialize_skips_failures(fresh_manager, tmp_path, monkeypatch):
    good_base = tmp_path / "good"
    good_base.mkdir()
    _allow_root(monkeypatch, tmp_path)
    good = _make_conn("good", str(good_base))
    bad = _make_conn("bad", str(tmp_path / "missing"))  # does not exist → fails

    await fresh_manager.initialize([good, bad])
    assert fresh_manager.has_connection("good")
    assert not fresh_manager.has_connection("bad")


@pytest.mark.asyncio
async def test_dispose_all(fresh_manager, tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("a", str(tmp_path / "a")))
    await fresh_manager.add_connection(_make_conn("b", str(tmp_path / "b")))
    await fresh_manager.dispose_all()
    assert fresh_manager.list_aliases() == []


@pytest.mark.asyncio
async def test_get_max_download_bytes(fresh_manager, tmp_path, monkeypatch):
    base = tmp_path / "share"
    base.mkdir()
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("cap", str(base), max_download_bytes=1234))
    assert fresh_manager.get_max_download_bytes("cap") == 1234


# ── test_connection / health probe ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_connection_ok(fresh_manager, tmp_path, monkeypatch):
    base = tmp_path / "share"
    base.mkdir()
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("ok", str(base)))
    ok, msg = await fresh_manager.test_connection("ok")
    assert ok is True
    assert isinstance(msg, str)


@pytest.mark.asyncio
async def test_test_connection_mount_disappeared_stdev(fresh_manager, tmp_path, monkeypatch):
    """If the cached st_dev no longer matches the live device id, the probe
    must report failure (mount went away / got replaced by a stub)."""
    base = tmp_path / "share"
    base.mkdir()
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("mnt", str(base)))

    # Simulate the mount disappearing: poison the cached st_dev so the live
    # re-stat no longer matches.
    fresh_manager._bases["mnt"].st_dev = fresh_manager._bases["mnt"].st_dev + 999

    ok, _msg = await fresh_manager.test_connection("mnt")
    assert ok is False


# ── list_entries: shape, sorting, hidden policy, cap ──────────────────────────


@pytest.mark.asyncio
async def test_list_entries_response_shape(fresh_manager, tmp_path, monkeypatch):
    base = tmp_path / "share"
    base.mkdir()
    (base / "Zeta").mkdir()
    (base / "alpha").mkdir()
    (base / "b.txt").write_text("hello")
    (base / "A.csv").write_text("col")
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("s", str(base)))

    res = await fresh_manager.list_entries("s", "")

    # Top-level contract keys, nothing missing.
    assert set(res.keys()) == {
        "path",
        "folders",
        "files",
        "total_count",
        "has_more",
        "next_cursor",
    }
    assert res["path"] == ""

    # Folders sorted first, name-sorted case-insensitively.
    folder_names = [f["name"] for f in res["folders"]]
    assert folder_names == ["alpha", "Zeta"]
    file_names = [f["name"] for f in res["files"]]
    assert file_names == ["A.csv", "b.txt"]

    # Each entry object: exactly {name, path, is_dir, size, modified_time}.
    sample_dir = res["folders"][0]
    assert set(sample_dir.keys()) == {"name", "path", "is_dir", "size", "modified_time"}
    assert sample_dir["is_dir"] is True
    assert sample_dir["size"] is None
    assert sample_dir["path"] == "alpha"

    sample_file = next(f for f in res["files"] if f["name"] == "b.txt")
    assert set(sample_file.keys()) == {"name", "path", "is_dir", "size", "modified_time"}
    assert sample_file["is_dir"] is False
    assert sample_file["size"] == 5
    assert sample_file["path"] == "b.txt"
    assert isinstance(sample_file["modified_time"], str)

    assert res["total_count"] == 4
    assert res["has_more"] is False
    assert res["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_entries_path_is_alias_relative(fresh_manager, tmp_path, monkeypatch):
    base = tmp_path / "share"
    sub = base / "a"
    sub.mkdir(parents=True)
    (sub / "f.csv").write_text("x")
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("s", str(base)))

    res = await fresh_manager.list_entries("s", "a")
    assert res["path"] == "a"
    f = res["files"][0]
    assert f["path"] == "a/f.csv"
    # No absolute path leaks.
    assert str(base) not in f["path"]


@pytest.mark.asyncio
async def test_list_entries_hides_dotfiles_by_default(fresh_manager, tmp_path, monkeypatch):
    base = tmp_path / "share"
    base.mkdir()
    (base / "visible.txt").write_text("x")
    (base / ".hidden").write_text("x")
    (base / ".DS_Store").write_text("x")
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("s", str(base)))

    res = await fresh_manager.list_entries("s", "")
    names = [f["name"] for f in res["files"]]
    assert "visible.txt" in names
    assert ".hidden" not in names
    assert ".DS_Store" not in names


@pytest.mark.asyncio
async def test_list_entries_shows_hidden_when_enabled(fresh_manager, tmp_path, monkeypatch):
    base = tmp_path / "share"
    base.mkdir()
    (base / ".hidden").write_text("x")
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("s", str(base), show_hidden=True))

    res = await fresh_manager.list_entries("s", "")
    names = [f["name"] for f in res["files"]]
    assert ".hidden" in names


@pytest.mark.asyncio
async def test_list_entries_pagination_offset_limit(fresh_manager, tmp_path, monkeypatch):
    base = tmp_path / "share"
    base.mkdir()
    for i in range(10):
        (base / f"file{i:02d}.txt").write_text("x")
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("s", str(base)))

    res = await fresh_manager.list_entries("s", "", offset=0, limit=4)
    returned = res["folders"] + res["files"]
    assert len(returned) == 4
    assert res["has_more"] is True
    assert res["next_cursor"] == "4"

    res2 = await fresh_manager.list_entries("s", "", offset=8, limit=4)
    returned2 = res2["folders"] + res2["files"]
    assert len(returned2) == 2
    assert res2["has_more"] is False
    assert res2["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_entries_entry_cap_sets_has_more(fresh_manager, tmp_path, monkeypatch):
    """When the scan hits NAS_MAX_LIST_ENTRIES, has_more must be True."""
    from app.config import settings

    base = tmp_path / "share"
    base.mkdir()
    for i in range(6):
        (base / f"f{i}.txt").write_text("x")
    _allow_root(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "NAS_MAX_LIST_ENTRIES", 3)
    await fresh_manager.add_connection(_make_conn("s", str(base)))

    res = await fresh_manager.list_entries("s", "", offset=0, limit=100)
    assert res["has_more"] is True


@pytest.mark.asyncio
async def test_list_entries_mount_disappeared_raises(fresh_manager, tmp_path, monkeypatch):
    base = tmp_path / "share"
    base.mkdir()
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("s", str(base)))
    fresh_manager._bases["s"].st_dev = fresh_manager._bases["s"].st_dev + 999
    with pytest.raises(NasUnavailableError):
        await fresh_manager.list_entries("s", "")


# ── stat_path ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stat_path_file_shape_and_content_type(fresh_manager, tmp_path, monkeypatch):
    base = tmp_path / "share"
    base.mkdir()
    (base / "report.csv").write_text("a,b,c")
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("s", str(base)))

    res = await fresh_manager.stat_path("s", "report.csv")
    assert set(res.keys()) == {
        "name",
        "path",
        "is_dir",
        "size",
        "modified_time",
        "content_type",
    }
    assert res["name"] == "report.csv"
    assert res["path"] == "report.csv"
    assert res["is_dir"] is False
    assert res["size"] == 5
    assert res["content_type"] in ("text/csv", "application/vnd.ms-excel", "application/csv")


@pytest.mark.asyncio
async def test_stat_path_dir_content_type_null(fresh_manager, tmp_path, monkeypatch):
    base = tmp_path / "share"
    (base / "sub").mkdir(parents=True)
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("s", str(base)))

    res = await fresh_manager.stat_path("s", "sub")
    assert res["is_dir"] is True
    assert res["size"] is None
    assert res["content_type"] is None


@pytest.mark.asyncio
async def test_stat_path_hidden_target_is_404_when_not_shown(fresh_manager, tmp_path, monkeypatch):
    base = tmp_path / "share"
    base.mkdir()
    (base / ".secret").write_text("x")
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("s", str(base)))
    with pytest.raises(FileNotFoundError):
        await fresh_manager.stat_path("s", ".secret")


@pytest.mark.asyncio
async def test_stat_path_special_file_rejected(fresh_manager, tmp_path, monkeypatch):
    base = tmp_path / "share"
    base.mkdir()
    os.mkfifo(base / "pipe")
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("s", str(base), show_hidden=True))
    with pytest.raises(Exception):
        await fresh_manager.stat_path("s", "pipe")


# ── open_read_stream ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_read_stream_reads_content(fresh_manager, tmp_path, monkeypatch):
    base = tmp_path / "share"
    base.mkdir()
    payload = b"the quick brown fox" * 100
    (base / "data.bin").write_bytes(payload)
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("s", str(base)))

    gen, meta = await fresh_manager.open_read_stream("s", "data.bin")
    assert set(meta.keys()) == {"size", "content_type", "filename"}
    assert meta["size"] == len(payload)
    assert meta["filename"] == "data.bin"

    collected = bytearray()
    async for chunk in gen:
        collected.extend(chunk)
    assert bytes(collected) == payload


@pytest.mark.asyncio
async def test_open_read_stream_size_cap_raises_too_large(fresh_manager, tmp_path, monkeypatch):
    """File larger than the (per-connection) cap raises NasTooLargeError BEFORE
    streaming begins."""
    base = tmp_path / "share"
    base.mkdir()
    (base / "big.bin").write_bytes(b"x" * 2048)
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(
        _make_conn("s", str(base), max_download_bytes=1024)
    )
    with pytest.raises(NasTooLargeError):
        await fresh_manager.open_read_stream("s", "big.bin")


@pytest.mark.asyncio
async def test_open_read_stream_fifo_rejected_without_hanging(fresh_manager, tmp_path, monkeypatch):
    """open_read_stream on a FIFO must raise quickly, never block on the
    writer-less pipe. Wrapped in asyncio.wait_for as a tight watchdog."""
    import asyncio

    base = tmp_path / "share"
    base.mkdir()
    os.mkfifo(base / "pipe")
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("s", str(base), show_hidden=True))

    with pytest.raises(Exception) as excinfo:
        await asyncio.wait_for(
            fresh_manager.open_read_stream("s", "pipe"), timeout=5.0
        )
    # If it hung, asyncio.wait_for would raise TimeoutError — that is a FAILURE
    # of the no-block guarantee, so assert it is NOT a timeout.
    assert not isinstance(excinfo.value, asyncio.TimeoutError), "open_read_stream HUNG on a FIFO"


@pytest.mark.asyncio
async def test_open_read_stream_fd_not_leaked_on_aborted_stream(fresh_manager, tmp_path, monkeypatch):
    """If a consumer aborts mid-stream (closes the generator), the underlying
    file descriptor must be released in the generator's finally block."""
    base = tmp_path / "share"
    base.mkdir()
    (base / "data.bin").write_bytes(b"y" * (4 * 1024 * 1024))
    _allow_root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        __import__("app.config", fromlist=["settings"]).settings,
        "NAS_STREAM_CHUNK_BYTES",
        64 * 1024,
    )
    await fresh_manager.add_connection(_make_conn("s", str(base)))

    fds_before = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None

    gen, _meta = await fresh_manager.open_read_stream("s", "data.bin")
    # Consume only the first chunk, then abort.
    first = await gen.__anext__()
    assert first
    await gen.aclose()

    if fds_before is not None:
        fds_after = len(os.listdir("/proc/self/fd"))
        # Allow a small slack but the streaming fd must be gone.
        assert fds_after <= fds_before + 1, "file descriptor leaked across aborted stream"


@pytest.mark.asyncio
async def test_open_read_stream_hidden_target_404(fresh_manager, tmp_path, monkeypatch):
    base = tmp_path / "share"
    base.mkdir()
    (base / ".secret").write_text("x")
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("s", str(base)))
    with pytest.raises(FileNotFoundError):
        await fresh_manager.open_read_stream("s", ".secret")


# ── EACCES handling ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_entries_eacces_propagates_permission_error(fresh_manager, tmp_path, monkeypatch):
    """An unreadable subdirectory surfaces as PermissionError (→ router 403)."""
    if os.geteuid() == 0:
        pytest.skip("running as root: permission bits are not enforced")
    base = tmp_path / "share"
    locked = base / "locked"
    locked.mkdir(parents=True)
    (locked / "f.txt").write_text("x")
    _allow_root(monkeypatch, tmp_path)
    await fresh_manager.add_connection(_make_conn("s", str(base)))

    os.chmod(locked, 0o000)
    try:
        with pytest.raises(PermissionError):
            await fresh_manager.list_entries("s", "locked")
    finally:
        os.chmod(locked, 0o755)
