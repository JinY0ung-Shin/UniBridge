"""Deterministic edge-path tests for the NAS security primitives."""
from __future__ import annotations

import ctypes
import errno
import os

import pytest

from app.services import nas_security as nas


class _FakeLibc:
    def __init__(self, result: int, error: int = 0) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def syscall(self, *args) -> int:
        self.calls += 1
        ctypes.set_errno(self.error)
        return self.result


def test_resolve_base_skips_incomparable_allowed_root(tmp_path):
    base = tmp_path / "base"
    base.mkdir()

    with pytest.raises(nas.NasSecurityError, match="not permitted"):
        nas.resolve_base(str(base), ["relative/root"])


def test_resolve_base_maps_generic_resolve_error_to_unavailable(monkeypatch):
    class _InaccessiblePath:
        def resolve(self, *, strict):
            assert strict is True
            raise OSError("I/O failure")

    monkeypatch.setattr(nas, "Path", lambda _value: _InaccessiblePath())

    with pytest.raises(nas.NasUnavailableError, match="not available"):
        nas.resolve_base("/unavailable", ["/"])


def test_resolve_base_maps_stat_error_to_unavailable(monkeypatch, tmp_path):
    class _ResolvedPath:
        def resolve(self, *, strict):
            assert strict is True
            return tmp_path

    def _raise_stat_error(_path):
        raise OSError("stale mount")

    monkeypatch.setattr(nas, "Path", lambda _value: _ResolvedPath())
    monkeypatch.setattr(nas.os, "stat", _raise_stat_error)

    with pytest.raises(nas.NasUnavailableError, match="not available"):
        nas.resolve_base("/stale-mount", [str(tmp_path)])


def test_sanitize_relpath_rejects_none_and_drive_relative_path():
    with pytest.raises(nas.NasSecurityError, match="Invalid path"):
        nas.sanitize_relpath(None, 4096)  # type: ignore[arg-type]
    with pytest.raises(nas.NasSecurityError, match="Invalid path"):
        nas.sanitize_relpath("C:relative.txt", 4096)


def test_safe_resolve_missing_target_maps_to_file_not_found(tmp_path):
    base = tmp_path / "base"
    base.mkdir()

    with pytest.raises(FileNotFoundError, match="missing.txt"):
        nas.safe_resolve(base, "missing.txt", follow_symlinks=True)


def test_safe_resolve_maps_islink_oserror_to_file_not_found(monkeypatch, tmp_path):
    base = tmp_path / "base"
    base.mkdir()

    def _raise_islink(_path):
        raise OSError("directory entry disappeared")

    monkeypatch.setattr(nas.os.path, "islink", _raise_islink)

    with pytest.raises(FileNotFoundError, match="child"):
        nas.safe_resolve(base, "child", follow_symlinks=False)


def test_safe_resolve_maps_generic_resolve_oserror_to_file_not_found(monkeypatch):
    class _FailingPath:
        parts = ("base",)

        def joinpath(self, *_parts):
            return self

        def resolve(self, *, strict):
            assert strict is True
            raise OSError("filesystem failure")

        def __str__(self):
            return "/base"

    monkeypatch.setattr(nas, "Path", lambda _value: _FailingPath())

    with pytest.raises(FileNotFoundError, match="child"):
        nas.safe_resolve("/base", "child", follow_symlinks=True)


def test_safe_resolve_rejects_incomparable_commonpath(monkeypatch, tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("data")

    def _raise_commonpath(_paths):
        raise ValueError("different drives")

    monkeypatch.setattr(nas.os.path, "commonpath", _raise_commonpath)

    with pytest.raises(nas.NasSecurityError, match="Invalid path"):
        nas.safe_resolve(tmp_path, target.name, follow_symlinks=True)


def test_detect_openat2_caches_non_linux_result(monkeypatch):
    monkeypatch.setattr(nas, "_openat2_available", None)
    monkeypatch.setattr(nas.platform, "system", lambda: "Darwin")

    assert nas.openat2_supported() is False
    monkeypatch.setattr(
        nas.platform,
        "system",
        lambda: pytest.fail("cached capability should avoid probing again"),
    )
    assert nas.openat2_supported() is False


def test_detect_openat2_records_success(monkeypatch):
    probe_fd = os.open(os.devnull, os.O_RDONLY)
    fake_libc = _FakeLibc(probe_fd)
    monkeypatch.setattr(nas, "_openat2_available", None)
    monkeypatch.setattr(nas.platform, "system", lambda: "Linux")
    monkeypatch.setattr(nas.ctypes, "CDLL", lambda *_args, **_kwargs: fake_libc)

    assert nas.openat2_supported() is True
    assert fake_libc.calls == 1


def test_detect_openat2_records_probe_failure(monkeypatch):
    fake_libc = _FakeLibc(-1, errno.ENOSYS)
    monkeypatch.setattr(nas, "_openat2_available", None)
    monkeypatch.setattr(nas.platform, "system", lambda: "Linux")
    monkeypatch.setattr(nas.ctypes, "CDLL", lambda *_args, **_kwargs: fake_libc)

    assert nas.openat2_supported() is False
    assert fake_libc.calls == 1


def test_openat2_beneath_reports_unavailable(monkeypatch):
    monkeypatch.setattr(nas, "_detect_openat2", lambda: False)

    with pytest.raises(NotImplementedError, match="not available"):
        nas.openat2_beneath(-1, "file.txt")


@pytest.mark.parametrize(
    ("error", "exception"),
    [
        (errno.EXDEV, nas.NasSecurityError),
        (errno.ELOOP, nas.NasSecurityError),
        (errno.ENOENT, FileNotFoundError),
        (errno.EACCES, PermissionError),
        (errno.EIO, nas.NasSecurityError),
    ],
)
def test_openat2_beneath_maps_syscall_errors(monkeypatch, error, exception):
    fake_libc = _FakeLibc(-1, error)
    monkeypatch.setattr(nas, "_detect_openat2", lambda: True)
    monkeypatch.setattr(nas.ctypes, "CDLL", lambda *_args, **_kwargs: fake_libc)

    with pytest.raises(exception):
        nas.openat2_beneath(-1, "file.txt")


def test_openat2_beneath_returns_regular_fd(monkeypatch, tmp_path):
    target = tmp_path / "data.txt"
    target.write_text("hello")
    opened_fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
    fake_libc = _FakeLibc(opened_fd)
    monkeypatch.setattr(nas, "_detect_openat2", lambda: True)
    monkeypatch.setattr(nas.ctypes, "CDLL", lambda *_args, **_kwargs: fake_libc)

    fd = nas.openat2_beneath(-1, target.name)
    try:
        assert fd == opened_fd
        assert os.read(fd, 5) == b"hello"
    finally:
        os.close(fd)


def test_openat2_beneath_rejects_non_regular_fd(monkeypatch, tmp_path):
    opened_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    fake_libc = _FakeLibc(opened_fd)
    monkeypatch.setattr(nas, "_detect_openat2", lambda: True)
    monkeypatch.setattr(nas.ctypes, "CDLL", lambda *_args, **_kwargs: fake_libc)

    with pytest.raises(nas.NasSecurityError, match="unsupported file type"):
        nas.openat2_beneath(-1, ".")
    with pytest.raises(OSError):
        os.fstat(opened_fd)


def test_openat2_beneath_closes_fd_when_fstat_fails(monkeypatch, tmp_path):
    target = tmp_path / "data.txt"
    target.write_text("hello")
    opened_fd = os.open(target, os.O_RDONLY)
    fake_libc = _FakeLibc(opened_fd)
    monkeypatch.setattr(nas, "_detect_openat2", lambda: True)
    monkeypatch.setattr(nas.ctypes, "CDLL", lambda *_args, **_kwargs: fake_libc)

    def _raise_fstat(_fd):
        raise OSError("fstat failed")

    monkeypatch.setattr(nas.os, "fstat", _raise_fstat)

    with pytest.raises(OSError, match="fstat failed"):
        nas.openat2_beneath(-1, target.name)

    # The syscall-returned descriptor is closed even on classification failure.
    with pytest.raises(OSError):
        os.close(opened_fd)


def test_open_regular_fd_preserves_missing_file_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        nas.open_regular_fd(tmp_path / "missing", follow_symlinks=False)


def test_open_regular_fd_closes_fd_when_fstat_fails(monkeypatch, tmp_path):
    target = tmp_path / "data.txt"
    target.write_text("hello")
    opened_fds: list[int] = []
    real_open = os.open

    def _tracking_open(path, flags):
        fd = real_open(path, flags)
        opened_fds.append(fd)
        return fd

    def _raise_fstat(_fd):
        raise OSError("fstat failed")

    monkeypatch.setattr(nas.os, "open", _tracking_open)
    monkeypatch.setattr(nas.os, "fstat", _raise_fstat)

    with pytest.raises(OSError, match="fstat failed"):
        nas.open_regular_fd(target, follow_symlinks=False)

    assert len(opened_fds) == 1
    with pytest.raises(OSError):
        os.close(opened_fds[0])


class _BrokenDirEntry:
    def __init__(self, *, name="visible.txt", fail_at: str) -> None:
        self.name = name
        self.fail_at = fail_at

    def is_symlink(self):
        if self.fail_at == "is_symlink":
            raise OSError("entry disappeared")
        return False

    def is_dir(self, *, follow_symlinks):
        if self.fail_at == "type":
            raise OSError("entry disappeared")
        return False

    def is_file(self, *, follow_symlinks):
        return False


def test_classify_dirent_rejects_non_utf8_name():
    entry = _BrokenDirEntry(name="bad\udcff", fail_at="never")
    assert nas.classify_dirent(entry, show_hidden=True, follow_symlinks=True) is False


@pytest.mark.parametrize("fail_at", ["is_symlink", "type"])
def test_classify_dirent_hides_entry_when_metadata_lookup_fails(fail_at):
    entry = _BrokenDirEntry(fail_at=fail_at)
    assert nas.classify_dirent(entry, show_hidden=True, follow_symlinks=True) is False
