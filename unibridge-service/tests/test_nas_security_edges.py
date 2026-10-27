"""Deterministic edge-path tests for the NAS security primitives."""
from __future__ import annotations

import os

import pytest

from app.services import nas_security as nas


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
