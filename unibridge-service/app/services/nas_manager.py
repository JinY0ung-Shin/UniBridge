from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import stat
from collections.abc import AsyncGenerator, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, TypeVar

from app.config import settings
from app.models import NASConnection
from app.services.nas_security import (
    NasUnavailableError,
    NasTooLargeError,
    ResolvedBase,
    classify_dirent,
    open_regular_fd,
    parse_allowed_roots,
    resolve_base,
    safe_resolve,
    sanitize_relpath,
)

logger = logging.getLogger(__name__)

# Dedicated bounded executor: a stuck FS syscall (FIFO, dead NFS hard-mount)
# must NOT drain the shared asyncio.to_thread pool that S3/DB rely on.
_NAS_EXECUTOR: ThreadPoolExecutor | None = ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="nas-fs"
)

_T = TypeVar("_T")


def _get_executor() -> ThreadPoolExecutor:
    """Return the dedicated NAS executor, recreating it if it was disposed.

    dispose_all() shuts the executor down and clears it; a later op (a new
    app instance in-process, or the next test) lazily recreates it so the
    process-wide singleton never gets permanently wedged.
    """
    global _NAS_EXECUTOR
    if _NAS_EXECUTOR is None:
        _NAS_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="nas-fs")
    return _NAS_EXECUTOR


def _iso(ts: float | None) -> str | None:
    """Render a POSIX mtime as an aware ISO-8601 UTC string, or None."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class NASConnectionManager:
    """Singleton that manages read-only NAS / local-filesystem connections per alias."""

    _instance: NASConnectionManager | None = None
    _bases: dict[str, ResolvedBase]
    _configs: dict[str, dict[str, Any]]

    def __new__(cls) -> NASConnectionManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._bases = {}
            cls._instance._configs = {}
        return cls._instance

    # ---- blocking-op plumbing --------------------------------------------

    async def _run_blocking(self, fn: Callable[..., _T], *args: Any) -> _T:
        """Run a blocking FS op on the dedicated executor with a hard timeout.

        The per-op timeout means a hung NFS/FIFO syscall raises instead of
        wedging the request (and the dedicated pool isolates any leaked
        thread from the global to_thread pool).
        """
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(_get_executor(), fn, *args),
                timeout=settings.NAS_FS_OP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            logger.warning("NAS filesystem op timed out after %ss", settings.NAS_FS_OP_TIMEOUT_SECONDS)
            raise NasUnavailableError("Filesystem operation timed out") from exc

    # ---- registry lifecycle ----------------------------------------------

    async def initialize(self, connections: list[NASConnection]) -> None:
        for conn in connections:
            try:
                await self.add_connection(conn)
            except Exception:
                logger.exception("Failed to initialize NAS connection '%s'", conn.alias)

    async def add_connection(self, conn: NASConnection) -> None:
        if conn.alias in self._bases:
            await self.remove_connection(conn.alias)

        allowed_roots = parse_allowed_roots(settings.NAS_ALLOWED_ROOTS)
        # resolve_base validates existence + dir + allowed-root containment
        # (defense in depth with the schema validator). Runs off-loop because
        # realpath()/stat() touch the filesystem.
        resolved = await self._run_blocking(resolve_base, conn.base_path, allowed_roots)

        self._bases[conn.alias] = resolved
        self._configs[conn.alias] = {
            "base_path": conn.base_path,
            "max_download_bytes": conn.max_download_bytes,
            "show_hidden": bool(conn.show_hidden),
            "follow_symlinks": bool(conn.follow_symlinks),
        }
        logger.info("NAS connection registered for alias '%s'", conn.alias)
        logger.debug("NAS alias '%s' base resolved to %s", conn.alias, resolved.real_path)

    async def remove_connection(self, alias: str) -> None:
        self._bases.pop(alias, None)
        self._configs.pop(alias, None)
        logger.info("NAS connection removed for alias '%s'", alias)

    def has_connection(self, alias: str) -> bool:
        return alias in self._bases

    def list_aliases(self) -> list[str]:
        return list(self._bases.keys())

    def get_config(self, alias: str) -> dict[str, Any]:
        return self._configs.get(alias, {})

    def get_max_download_bytes(self, alias: str) -> int | None:
        return self._configs.get(alias, {}).get("max_download_bytes")

    # ---- internal helpers -------------------------------------------------

    def _require(self, alias: str) -> tuple[ResolvedBase, dict[str, Any]]:
        base = self._bases.get(alias)
        config = self._configs.get(alias)
        if base is None or config is None:
            raise KeyError(f"No NAS connection registered for alias '{alias}'")
        return base, config

    def _effective_cap(self, alias: str) -> int | None:
        """Tightest of the per-connection cap and the global ceiling.

        A per-connection cap may only LOWER the global hard ceiling.
        """
        global_cap = settings.NAS_MAX_DOWNLOAD_BYTES
        conn_cap = self.get_max_download_bytes(alias)
        if conn_cap is None:
            return global_cap
        if global_cap is None:
            return conn_cap
        return min(conn_cap, global_cap)

    def _probe_base(self, base: ResolvedBase) -> None:
        """Health probe: base must still exist, be a dir, and live on the same
        device it was registered on (mount-disappeared detection).

        Runs inside the executor (called via _run_blocking).
        """
        try:
            live = os.stat(base.real_path)
        except FileNotFoundError as exc:
            raise NasUnavailableError("NAS base path is unavailable") from exc
        except OSError as exc:
            raise NasUnavailableError("NAS base path is unavailable") from exc
        if not stat.S_ISDIR(live.st_mode):
            raise NasUnavailableError("NAS base path is unavailable")
        if live.st_dev != base.st_dev:
            # Device id changed → the mount went away and a stub dir is exposed.
            raise NasUnavailableError("NAS mount is unavailable")

    # ---- health -----------------------------------------------------------

    async def test_connection(self, alias: str) -> tuple[bool, str]:
        try:
            base, config = self._require(alias)
        except KeyError:
            return False, "Connection not registered"
        try:
            allowed_roots = parse_allowed_roots(settings.NAS_ALLOWED_ROOTS)

            def _check() -> None:
                # Re-resolve from the stored base_path so a re-pointed symlink
                # or vanished allowed-root is caught, then probe st_dev.
                fresh = resolve_base(config["base_path"], allowed_roots)
                if fresh.st_dev != base.st_dev:
                    raise NasUnavailableError("NAS mount is unavailable")

            await self._run_blocking(_check)
            return True, "Connection successful"
        except NasUnavailableError as exc:
            logger.warning("NAS connection test failed for '%s': %s", alias, exc)
            return False, "Connection failed"
        except Exception:
            logger.exception("NAS connection test failed for '%s'", alias)
            return False, "Connection failed"

    # ---- listing ----------------------------------------------------------

    async def list_entries(
        self,
        alias: str,
        relpath: str = "",
        *,
        offset: int = 0,
        limit: int | None = None,
        query: str = "",
    ) -> dict[str, Any]:
        base, config = self._require(alias)
        show_hidden: bool = config["show_hidden"]
        follow_symlinks: bool = config["follow_symlinks"]
        if limit is None:
            limit = settings.NAS_LIST_DEFAULT_LIMIT
        # Case-insensitive substring filter on the leaf name, scoped to the
        # current directory (non-recursive). Applied after the scan/sort so
        # pagination operates on the filtered set.
        needle = query.strip().casefold()

        def _list() -> dict[str, Any]:
            self._probe_base(base)
            # nas_security owns ALL path computation; we never build a path.
            target = safe_resolve(
                base.real_path, relpath, follow_symlinks=follow_symlinks
            )
            if not target.is_dir():
                raise NotADirectoryError("Not a directory")

            rel_clean = sanitize_relpath(relpath, settings.NAS_MAX_PATH_BYTES)
            rel_prefix = str(rel_clean) if str(rel_clean) != "." else ""

            max_scan = settings.NAS_MAX_LIST_ENTRIES
            folders: list[dict[str, Any]] = []
            files: list[dict[str, Any]] = []
            scanned = 0
            cap_hit = False

            with os.scandir(target) as it:
                for entry in it:
                    if scanned >= max_scan:
                        cap_hit = True
                        break
                    scanned += 1
                    if not classify_dirent(
                        entry, show_hidden=show_hidden, follow_symlinks=follow_symlinks
                    ):
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=follow_symlinks)
                        st = entry.stat(follow_symlinks=follow_symlinks)
                    except OSError:
                        # Race: entry vanished or became unreadable mid-scan.
                        continue
                    name = entry.name
                    child_path = f"{rel_prefix}/{name}" if rel_prefix else name
                    obj = {
                        "name": name,
                        "path": child_path,
                        "is_dir": is_dir,
                        "size": None if is_dir else int(st.st_size),
                        "modified_time": _iso(st.st_mtime),
                    }
                    if is_dir:
                        folders.append(obj)
                    else:
                        files.append(obj)

            folders.sort(key=lambda e: e["name"].casefold())
            files.sort(key=lambda e: e["name"].casefold())

            if needle:
                folders = [e for e in folders if needle in e["name"].casefold()]
                files = [e for e in files if needle in e["name"].casefold()]

            combined = folders + files

            window = combined[offset : offset + limit]
            page_folders = [e for e in window if e["is_dir"]]
            page_files = [e for e in window if not e["is_dir"]]

            # Paging is purely window-based over the (filtered) scanned set, so
            # it always terminates. cap_hit is reported separately as `truncated`
            # — offset paging re-scans from the start each call and can never
            # reach entries beyond the scan cap, so it must NOT extend has_more
            # (doing so produced an endless "Load More" yielding empty pages).
            has_more = (offset + limit) < len(combined)
            next_cursor = str(offset + limit) if has_more else None

            return {
                "path": rel_prefix,
                "folders": page_folders,
                "files": page_files,
                "total_count": len(window),
                "has_more": has_more,
                "next_cursor": next_cursor,
                "truncated": cap_hit,
            }

        return await self._run_blocking(_list)

    # ---- stat -------------------------------------------------------------

    async def stat_path(self, alias: str, relpath: str) -> dict[str, Any]:
        base, config = self._require(alias)
        show_hidden: bool = config["show_hidden"]
        follow_symlinks: bool = config["follow_symlinks"]

        def _stat() -> dict[str, Any]:
            self._probe_base(base)
            rel_clean = sanitize_relpath(relpath, settings.NAS_MAX_PATH_BYTES)
            name = rel_clean.name
            # Existence-hiding: a hidden target is reported as not-found.
            if not show_hidden and _is_hidden_name(name):
                raise FileNotFoundError("Not found")

            target = safe_resolve(
                base.real_path, relpath, follow_symlinks=follow_symlinks
            )
            st = os.stat(target)
            mode = st.st_mode
            is_dir = stat.S_ISDIR(mode)
            if not (is_dir or stat.S_ISREG(mode)):
                # Whitelist regular files and dirs only.
                raise FileNotFoundError("Not found")

            content_type = None if is_dir else (mimetypes.guess_type(name)[0])
            return {
                "name": name,
                "path": str(rel_clean),
                "is_dir": is_dir,
                "size": None if is_dir else int(st.st_size),
                "modified_time": _iso(st.st_mtime),
                "content_type": content_type,
            }

        return await self._run_blocking(_stat)

    # ---- download / stream ------------------------------------------------

    async def open_read_stream(
        self,
        alias: str,
        relpath: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> tuple[AsyncGenerator[bytes, None], dict[str, Any]]:
        base, config = self._require(alias)
        show_hidden: bool = config["show_hidden"]
        follow_symlinks: bool = config["follow_symlinks"]
        cap = self._effective_cap(alias)
        chunk_size = settings.NAS_STREAM_CHUNK_BYTES

        def _open() -> tuple[int, dict[str, Any]]:
            self._probe_base(base)
            rel_clean = sanitize_relpath(relpath, settings.NAS_MAX_PATH_BYTES)
            name = rel_clean.name
            # Existence-hiding for hidden targets.
            if not show_hidden and _is_hidden_name(name):
                raise FileNotFoundError("Not found")

            target = safe_resolve(
                base.real_path, relpath, follow_symlinks=follow_symlinks
            )
            # open_regular_fd uses O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC and fstats
            # AFTER open, so a FIFO with no writer cannot block forever and a
            # non-regular file is rejected fast.
            fd = open_regular_fd(target, follow_symlinks=follow_symlinks)
            try:
                st = os.fstat(fd)
                size = int(st.st_size)
                if cap is not None and size > cap:
                    raise NasTooLargeError("File exceeds the maximum download size")
                content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                meta = {"size": size, "content_type": content_type, "filename": name}
            except BaseException:
                os.close(fd)
                raise
            return fd, meta

        fd, meta = await self._run_blocking(_open)

        async def _gen() -> AsyncGenerator[bytes, None]:
            try:
                if offset:
                    await self._run_blocking(os.lseek, fd, offset, os.SEEK_SET)
                remaining = length
                while True:
                    to_read = chunk_size
                    if remaining is not None:
                        if remaining <= 0:
                            break
                        to_read = min(chunk_size, remaining)
                    data = await self._run_blocking(os.read, fd, to_read)
                    if not data:
                        break
                    if remaining is not None:
                        remaining -= len(data)
                    yield data
            finally:
                # Always close the fd, even on client abort / exception.
                try:
                    os.close(fd)
                except OSError:
                    pass

        return _gen(), meta

    # ---- shutdown ---------------------------------------------------------

    async def dispose_all(self) -> None:
        global _NAS_EXECUTOR
        for alias in list(self._bases.keys()):
            await self.remove_connection(alias)
        if _NAS_EXECUTOR is not None:
            _NAS_EXECUTOR.shutdown(wait=False, cancel_futures=True)
            _NAS_EXECUTOR = None


def _is_hidden_name(name: str) -> bool:
    """A leaf name is hidden if it begins with a dot (POSIX dotfile)."""
    return name.startswith(".")


nas_manager = NASConnectionManager()
