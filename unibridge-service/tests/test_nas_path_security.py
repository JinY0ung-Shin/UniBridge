"""Unit tests for the NAS security kernel (app.services.nas_security).

These tests use REAL temp directories, REAL symlinks (os.symlink) and a REAL
FIFO (os.mkfifo) — NOT mocks — because the whole point of the security kernel is
to defend against filesystem-level traversal/escape/special-file attacks that
only manifest against a real filesystem.
"""
from __future__ import annotations

import os
import socket
import stat
import threading

import pytest

from app.services.nas_security import (
    NasSecurityError,
    NasTooLargeError,  # noqa: F401  (imported to assert the public name exists)
    NasUnavailableError,
    ResolvedBase,
    classify_dirent,
    open_regular_fd,
    parse_allowed_roots,
    resolve_base,
    safe_resolve,
    sanitize_relpath,
)

# A generous-but-finite path-byte cap mirroring settings.NAS_MAX_PATH_BYTES.
MAX_PATH_BYTES = 4096


# ── helpers ──────────────────────────────────────────────────────────────────


def _allowed(base) -> list[str]:
    """Allowed-roots list that contains the realpath of ``base``."""
    return [os.path.realpath(str(base))]


# ── parse_allowed_roots ───────────────────────────────────────────────────────


def test_parse_allowed_roots_splits_and_strips():
    roots = parse_allowed_roots("/mnt , /data,/srv/share ")
    assert roots == ["/mnt", "/data", "/srv/share"]


def test_parse_allowed_roots_drops_empty_segments():
    assert parse_allowed_roots("/mnt,,, ,/data") == ["/mnt", "/data"]


# ── resolve_base ──────────────────────────────────────────────────────────────


def test_resolve_base_returns_resolved_base(tmp_path):
    base = tmp_path / "share"
    base.mkdir()
    resolved = resolve_base(str(base), _allowed(tmp_path))
    assert isinstance(resolved, ResolvedBase)
    assert resolved.real_path == base.resolve()
    assert resolved.st_dev == os.stat(base).st_dev


def test_resolve_base_missing_raises_unavailable(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(NasUnavailableError):
        resolve_base(str(missing), _allowed(tmp_path))


def test_resolve_base_not_a_dir_raises_unavailable(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(NasUnavailableError):
        resolve_base(str(f), _allowed(tmp_path))


def test_resolve_base_outside_allowed_roots_rejected(tmp_path):
    """base_path NOT under any NAS_ALLOWED_ROOTS prefix must be rejected."""
    base = tmp_path / "share"
    base.mkdir()
    # allowed roots points somewhere unrelated
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(NasSecurityError):
        resolve_base(str(base), [os.path.realpath(str(other))])


def test_resolve_base_uses_realpath_for_symlinked_mount(tmp_path):
    """A base that is itself a symlink to a real dir must resolve to the real
    target, and containment must be evaluated on the REAL path."""
    real = tmp_path / "real_mount"
    real.mkdir()
    link = tmp_path / "mount_link"
    os.symlink(real, link)
    resolved = resolve_base(str(link), _allowed(tmp_path))
    assert resolved.real_path == real.resolve()


def test_resolve_base_sibling_prefix_not_confused(tmp_path):
    """commonpath (not str.startswith) must be used: /mnt/data is NOT under
    an allowed root of /mnt/da even though the string prefixes match."""
    allowed_root = tmp_path / "da"
    allowed_root.mkdir()
    sneaky = tmp_path / "data"
    sneaky_path = sneaky
    sneaky_path.mkdir()
    with pytest.raises(NasSecurityError):
        resolve_base(str(sneaky_path), [os.path.realpath(str(allowed_root))])


# ── sanitize_relpath ──────────────────────────────────────────────────────────


def test_sanitize_relpath_simple_ok():
    cleaned = sanitize_relpath("a/b/c.txt", MAX_PATH_BYTES)
    assert str(cleaned) == "a/b/c.txt"


def test_sanitize_relpath_empty_ok():
    cleaned = sanitize_relpath("", MAX_PATH_BYTES)
    assert str(cleaned) in ("", ".")


def test_sanitize_relpath_rejects_parent_traversal():
    with pytest.raises(NasSecurityError):
        sanitize_relpath("../../etc/passwd", MAX_PATH_BYTES)


def test_sanitize_relpath_rejects_embedded_parent():
    with pytest.raises(NasSecurityError):
        sanitize_relpath("a/../../b", MAX_PATH_BYTES)


def test_sanitize_relpath_rejects_dot_segment():
    with pytest.raises(NasSecurityError):
        sanitize_relpath("a/./b", MAX_PATH_BYTES)


def test_sanitize_relpath_rejects_absolute():
    with pytest.raises(NasSecurityError):
        sanitize_relpath("/etc/passwd", MAX_PATH_BYTES)


def test_sanitize_relpath_rejects_backslash():
    with pytest.raises(NasSecurityError):
        sanitize_relpath("a\\b", MAX_PATH_BYTES)


def test_sanitize_relpath_rejects_windows_drive():
    with pytest.raises(NasSecurityError):
        sanitize_relpath("C:\\Windows", MAX_PATH_BYTES)


def test_sanitize_relpath_rejects_unc():
    with pytest.raises(NasSecurityError):
        sanitize_relpath("\\\\server\\share", MAX_PATH_BYTES)


def test_sanitize_relpath_rejects_nul_byte():
    with pytest.raises(NasSecurityError):
        sanitize_relpath("a/b\x00c", MAX_PATH_BYTES)


def test_sanitize_relpath_rejects_overlength():
    with pytest.raises(NasSecurityError):
        sanitize_relpath("a" * (MAX_PATH_BYTES + 1), MAX_PATH_BYTES)


# ── safe_resolve ──────────────────────────────────────────────────────────────


def test_safe_resolve_simple_within_base(tmp_path):
    base = tmp_path / "base"
    (base / "sub").mkdir(parents=True)
    target = safe_resolve(base.resolve(), "sub", follow_symlinks=False)
    assert target == (base / "sub").resolve()


def test_safe_resolve_traversal_blocked(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    with pytest.raises(NasSecurityError):
        safe_resolve(base.resolve(), "../../etc/passwd", follow_symlinks=False)


def test_safe_resolve_symlink_escape_to_etc_blocked(tmp_path):
    """A symlink inside base pointing OUT of base (to /etc) must be rejected."""
    base = tmp_path / "base"
    base.mkdir()
    escape = base / "escape"
    os.symlink("/etc", escape)
    with pytest.raises(NasSecurityError):
        safe_resolve(base.resolve(), "escape/passwd", follow_symlinks=False)


def test_safe_resolve_symlink_rejected_when_not_following(tmp_path):
    """Even a symlink that stays INSIDE base is rejected when follow_symlinks
    is False (no-follow is the hardened default)."""
    base = tmp_path / "base"
    real = base / "realdir"
    real.mkdir(parents=True)
    (real / "f.txt").write_text("hi")
    link = base / "link"
    os.symlink(real, link)
    with pytest.raises(NasSecurityError):
        safe_resolve(base.resolve(), "link/f.txt", follow_symlinks=False)


def test_safe_resolve_internal_symlink_allowed_when_following(tmp_path):
    """A symlink that stays within base is allowed when follow_symlinks=True."""
    base = tmp_path / "base"
    real = base / "realdir"
    real.mkdir(parents=True)
    (real / "f.txt").write_text("hi")
    link = base / "link"
    os.symlink(real, link)
    target = safe_resolve(base.resolve(), "link/f.txt", follow_symlinks=True)
    assert target == (real / "f.txt").resolve()


def test_safe_resolve_symlink_escape_blocked_even_when_following(tmp_path):
    """follow_symlinks=True still must NOT let a symlink escape base
    (real-path containment is enforced regardless)."""
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s")
    link = base / "out"
    os.symlink(outside, link)
    with pytest.raises(NasSecurityError):
        safe_resolve(base.resolve(), "out/secret", follow_symlinks=True)


def test_safe_resolve_escape_via_intermediate_symlinked_dir(tmp_path):
    """An intermediate directory component that is a symlink escaping base
    must be rejected."""
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (outside / "deep" / "x").write_text("x")
    os.symlink(outside, base / "bridge")
    with pytest.raises(NasSecurityError):
        safe_resolve(base.resolve(), "bridge/deep/x", follow_symlinks=False)


def test_safe_resolve_broken_symlink(tmp_path):
    """A broken (dangling) symlink must not silently resolve; it should raise a
    security error (not-following) or a FileNotFoundError — never escape."""
    base = tmp_path / "base"
    base.mkdir()
    os.symlink(base / "does-not-exist", base / "broken")
    with pytest.raises((NasSecurityError, FileNotFoundError)):
        safe_resolve(base.resolve(), "broken", follow_symlinks=False)


# ── open_regular_fd ───────────────────────────────────────────────────────────


def test_open_regular_fd_regular_file(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("hello")
    fd = open_regular_fd(f.resolve(), follow_symlinks=False)
    try:
        assert os.fstat(fd).st_size == 5
        assert stat.S_ISREG(os.fstat(fd).st_mode)
    finally:
        os.close(fd)


def test_open_regular_fd_directory_rejected(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    with pytest.raises(NasSecurityError):
        open_regular_fd(d.resolve(), follow_symlinks=False)


def test_open_regular_fd_fifo_rejected_without_blocking(tmp_path):
    """THE critical case: a FIFO with no writer must NOT make os.open() hang
    forever. open_regular_fd uses O_NONBLOCK so the open returns immediately,
    fstat sees a non-regular file, and it raises NasSecurityError fast.

    We run it on a watchdog thread with a tight timeout; if it hasn't completed
    quickly the test fails (rather than hanging the whole suite).
    """
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)

    result: dict[str, object] = {}

    def _attempt():
        try:
            fd = open_regular_fd(fifo.resolve(), follow_symlinks=False)
            # Should never reach here; if it does, clean up so we don't leak.
            os.close(fd)
            result["outcome"] = "opened"
        except NasSecurityError:
            result["outcome"] = "rejected"
        except Exception as exc:  # pragma: no cover - any non-hang failure is informative
            result["outcome"] = f"error:{type(exc).__name__}"

    worker = threading.Thread(target=_attempt, daemon=True)
    worker.start()
    worker.join(timeout=5.0)

    assert not worker.is_alive(), "open_regular_fd BLOCKED on a FIFO (no O_NONBLOCK?)"
    assert result.get("outcome") == "rejected", result


def test_open_regular_fd_socket_rejected(tmp_path):
    sock_path = tmp_path / "s.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock_path))
        with pytest.raises(NasSecurityError):
            fd = open_regular_fd(sock_path.resolve(), follow_symlinks=False)
            os.close(fd)  # pragma: no cover
    finally:
        srv.close()


def test_open_regular_fd_char_device_rejected():
    """/dev/null is a character device and must be rejected (whitelist S_ISREG)."""
    if not os.path.exists("/dev/null"):
        pytest.skip("/dev/null not available")
    from pathlib import Path

    with pytest.raises(NasSecurityError):
        fd = open_regular_fd(Path("/dev/null"), follow_symlinks=False)
        os.close(fd)  # pragma: no cover


# ── classify_dirent ───────────────────────────────────────────────────────────


def _scandir_entry(directory, name):
    for entry in os.scandir(directory):
        if entry.name == name:
            return entry
    raise AssertionError(f"no dirent named {name}")


def test_classify_dirent_includes_regular_file(tmp_path):
    (tmp_path / "data.csv").write_text("x")
    entry = _scandir_entry(tmp_path, "data.csv")
    assert classify_dirent(entry, show_hidden=False, follow_symlinks=False) is True


def test_classify_dirent_includes_subdir(tmp_path):
    (tmp_path / "sub").mkdir()
    entry = _scandir_entry(tmp_path, "sub")
    assert classify_dirent(entry, show_hidden=False, follow_symlinks=False) is True


def test_classify_dirent_drops_dotfile_by_default(tmp_path):
    (tmp_path / ".secret").write_text("x")
    entry = _scandir_entry(tmp_path, ".secret")
    assert classify_dirent(entry, show_hidden=False, follow_symlinks=False) is False


def test_classify_dirent_includes_dotfile_when_show_hidden(tmp_path):
    (tmp_path / ".secret").write_text("x")
    entry = _scandir_entry(tmp_path, ".secret")
    assert classify_dirent(entry, show_hidden=True, follow_symlinks=False) is True


def test_classify_dirent_drops_os_junk(tmp_path):
    (tmp_path / ".DS_Store").write_text("x")
    (tmp_path / "Thumbs.db").write_text("x")
    ds = _scandir_entry(tmp_path, ".DS_Store")
    thumbs = _scandir_entry(tmp_path, "Thumbs.db")
    assert classify_dirent(ds, show_hidden=False, follow_symlinks=False) is False
    assert classify_dirent(thumbs, show_hidden=False, follow_symlinks=False) is False


def test_classify_dirent_drops_git(tmp_path):
    (tmp_path / ".git").mkdir()
    entry = _scandir_entry(tmp_path, ".git")
    assert classify_dirent(entry, show_hidden=False, follow_symlinks=False) is False


def test_classify_dirent_drops_symlink_when_not_following(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("x")
    os.symlink(target, tmp_path / "link.txt")
    entry = _scandir_entry(tmp_path, "link.txt")
    assert classify_dirent(entry, show_hidden=False, follow_symlinks=False) is False


def test_classify_dirent_keeps_symlink_when_following(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("x")
    os.symlink(target, tmp_path / "link.txt")
    entry = _scandir_entry(tmp_path, "link.txt")
    assert classify_dirent(entry, show_hidden=False, follow_symlinks=True) is True


def test_classify_dirent_drops_fifo_always(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    entry = _scandir_entry(tmp_path, "pipe")
    # FIFOs are dropped even with show_hidden + follow_symlinks set.
    assert classify_dirent(entry, show_hidden=True, follow_symlinks=True) is False


def test_classify_dirent_drops_socket_always(tmp_path):
    sock_path = tmp_path / "s.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock_path))
        entry = _scandir_entry(tmp_path, "s.sock")
        assert classify_dirent(entry, show_hidden=True, follow_symlinks=True) is False
    finally:
        srv.close()
