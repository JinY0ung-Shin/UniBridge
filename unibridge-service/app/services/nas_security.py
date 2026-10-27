from __future__ import annotations

import errno
import fcntl
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class NasSecurityError(ValueError):
    """Traversal / symlink-escape / bad type / bad input -> router 400.

    Subclasses ``ValueError`` so callers can map both to a 400 ("Invalid path").
    Messages MUST NOT contain absolute base_path/real paths (client-facing).
    """


class NasUnavailableError(Exception):
    """Mount gone / base inaccessible -> router 503."""


class NasTooLargeError(Exception):
    """File exceeds the download cap -> router 413."""


# --------------------------------------------------------------------------- #
# Resolved base record
# --------------------------------------------------------------------------- #
@dataclass
class ResolvedBase:
    """A base_path that has been fully realpath-resolved and validated."""

    real_path: Path  # base_path fully realpath-resolved (mount symlinks collapsed), cached once
    st_dev: int      # device id of the mounted fs at add time (mount-disappeared detection)


# --------------------------------------------------------------------------- #
# Junk / hidden filtering
# --------------------------------------------------------------------------- #
# OS-junk filenames dropped from listings (and treated as hidden) unless
# show_hidden is enabled. Dotfiles are filtered separately by their leading '.'.
_OS_JUNK_NAMES = frozenset({
    ".DS_Store",
    "Thumbs.db",
    "$RECYCLE.BIN",
    ".git",
})


def _is_hidden_name(name: str) -> bool:
    """A name is hidden if it starts with '.' or is known OS junk."""
    return name.startswith(".") or name in _OS_JUNK_NAMES


def _name_is_utf8_clean(name: str) -> bool:
    """Reject names that fail a utf-8 surrogateescape round-trip.

    ``os.listdir``/``os.scandir`` decode bytes with surrogateescape, so a name
    containing undecodable bytes carries lone surrogates that cannot be encoded
    back to clean utf-8. Such entries are dropped to avoid emitting broken JSON.
    """
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# --------------------------------------------------------------------------- #
# Allowed roots / base resolution
# --------------------------------------------------------------------------- #
def parse_allowed_roots(raw: str) -> list[str]:
    """Split a comma-separated NAS_ALLOWED_ROOTS string into absolute prefixes.

    Whitespace is trimmed and empty segments dropped. Each root is realpath-
    resolved so containment checks compare real paths to real paths.
    """
    roots: list[str] = []
    for chunk in (raw or "").split(","):
        candidate = chunk.strip()
        if not candidate:
            continue
        roots.append(str(Path(candidate).resolve()))
    return roots


def _is_under_allowed_root(target_real: Path, allowed_roots: list[str]) -> bool:
    """True if ``target_real`` equals or is contained by an allowed root.

    Uses ``os.path.commonpath`` on real paths (never ``str.startswith``, which
    would treat ``/mnt-evil`` as under ``/mnt``).
    """
    target_str = str(target_real)
    for root in allowed_roots:
        try:
            if os.path.commonpath([root, target_str]) == root:
                return True
        except ValueError:
            # Different drives / mixed absolute-relative; not comparable.
            continue
    return False


def resolve_base(base_path: str, allowed_roots: list[str]) -> ResolvedBase:
    """Resolve and validate a connection's base_path.

    - ``Path(base_path).resolve(strict=True)`` (mount symlinks collapsed).
    - Assert the resolved target is a directory.
    - Assert the real path sits under one of ``allowed_roots`` (commonpath on
      real paths, never ``str.startswith``).

    Raises ``NasUnavailableError`` if missing / not a directory / inaccessible,
    ``NasSecurityError`` if the resolved path is outside the allowed roots.
    """
    try:
        real_path = Path(base_path).resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as exc:
        logger.debug("resolve_base: base_path not found: %s (%s)", base_path, exc)
        raise NasUnavailableError("base path is not available") from exc
    except OSError as exc:
        logger.debug("resolve_base: base_path inaccessible: %s (%s)", base_path, exc)
        raise NasUnavailableError("base path is not available") from exc

    try:
        st = os.stat(real_path)
    except OSError as exc:
        logger.debug("resolve_base: stat failed for %s (%s)", real_path, exc)
        raise NasUnavailableError("base path is not available") from exc

    if not stat.S_ISDIR(st.st_mode):
        logger.debug("resolve_base: base_path is not a directory: %s", real_path)
        raise NasUnavailableError("base path is not a directory")

    if not _is_under_allowed_root(real_path, allowed_roots):
        logger.debug(
            "resolve_base: base_path %s (real %s) outside allowed roots %s",
            base_path, real_path, allowed_roots,
        )
        raise NasSecurityError("base path is not permitted")

    return ResolvedBase(real_path=real_path, st_dev=st.st_dev)


# --------------------------------------------------------------------------- #
# Relative-path sanitisation
# --------------------------------------------------------------------------- #
def sanitize_relpath(relpath: str, max_bytes: int) -> PurePosixPath:
    """Validate and clean a client-supplied alias-relative path.

    Rejects (raising ``NasSecurityError``): NUL byte, byte-length > ``max_bytes``,
    backslash, absolute paths, Windows drive/UNC prefixes, and any ``..`` or ``.``
    segment. An empty / "." path normalises to the base itself (``PurePosixPath()``).
    """
    if relpath is None:
        raise NasSecurityError("Invalid path")
    if "\x00" in relpath:
        raise NasSecurityError("Invalid path")
    if len(relpath.encode("utf-8", "surrogatepass")) > max_bytes:
        raise NasSecurityError("path too long")
    if "\\" in relpath:
        raise NasSecurityError("Invalid path")

    stripped = relpath.strip()
    if stripped in ("", ".", "/"):
        return PurePosixPath()

    # Windows drive letter (C:) or UNC (\\server) prefixes.
    if len(stripped) >= 2 and stripped[1] == ":" and stripped[0].isalpha():
        raise NasSecurityError("Invalid path")

    # Inspect the RAW slash-delimited segments before PurePosixPath collapses
    # them: ``PurePosixPath('a/./b').parts`` drops the '.' silently, so a check
    # against ``.parts`` alone would miss embedded '.' / empty segments. A single
    # trailing slash (``sub/``) is harmless and tolerated; any other empty
    # segment (leading '/', or a '//' collapse) is rejected as ambiguous.
    body = stripped[:-1] if stripped.endswith("/") and len(stripped) > 1 else stripped
    for seg in body.split("/"):
        if seg == "":
            raise NasSecurityError("Invalid path")
        if seg in ("..", "."):
            raise NasSecurityError("Invalid path")

    pure = PurePosixPath(stripped)
    if pure.is_absolute():
        raise NasSecurityError("Invalid path")

    for part in pure.parts:
        if part in ("..", "."):
            raise NasSecurityError("Invalid path")
        if part == "/" or part == "":
            raise NasSecurityError("Invalid path")

    return pure


# --------------------------------------------------------------------------- #
# Safe resolution with containment + symlink rejection
# --------------------------------------------------------------------------- #
def safe_resolve(base_real: Path, relpath: str, *, follow_symlinks: bool) -> Path:
    """Resolve ``base_real / relpath`` to a real path that is provably contained.

    - Sanitises ``relpath`` (uses a generous internal byte cap; callers should
      pre-sanitise via ``sanitize_relpath`` with the configured cap).
    - Joins onto ``base_real`` and resolves to a real path.
    - Asserts ``os.path.commonpath([base_real, target_real]) == str(base_real)``
      using REAL paths.
    - When ``follow_symlinks`` is False, walks every component of the joined
      (pre-resolution) path and rejects if any component is a symlink
      (``os.path.islink`` / ``lstat``).

    Raises ``NasSecurityError`` on traversal, symlink escape, or a rejected
    symlink component. Returns the resolved real ``Path``.
    """
    pure = sanitize_relpath(relpath, max_bytes=4096)
    base_real = Path(base_real)
    joined = base_real if not pure.parts else base_real.joinpath(*pure.parts)

    # Reject any symlink component (including the leaf) before resolving, so a
    # symlink that happens to point back inside the base is still refused.
    if not follow_symlinks:
        current = base_real
        for part in pure.parts:
            current = current / part
            try:
                if os.path.islink(current):
                    logger.debug("safe_resolve: symlink component rejected: %s", current)
                    raise NasSecurityError("Invalid path")
            except OSError as exc:
                # lstat failure on an intermediate component (e.g. ENOTDIR);
                # surface as a not-found at the manager layer.
                logger.debug("safe_resolve: lstat failed on %s (%s)", current, exc)
                raise FileNotFoundError(str(pure)) from exc

    try:
        target_real = joined.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        # Existence/type errors are NOT security errors; let the manager map
        # them to 404 (existence-hiding-friendly).
        raise FileNotFoundError(str(pure)) from None
    except OSError as exc:
        logger.debug("safe_resolve: resolve failed for %s (%s)", joined, exc)
        raise FileNotFoundError(str(pure)) from exc

    base_str = str(base_real)
    try:
        common = os.path.commonpath([base_str, str(target_real)])
    except ValueError:
        logger.debug("safe_resolve: commonpath incomparable base=%s target=%s", base_str, target_real)
        raise NasSecurityError("Invalid path") from None

    if common != base_str:
        logger.debug("safe_resolve: containment escape base=%s target=%s", base_str, target_real)
        raise NasSecurityError("Invalid path")

    return target_real


# --------------------------------------------------------------------------- #
# Regular-file fd opening (the FIFO-no-block fix)
# --------------------------------------------------------------------------- #
def _clear_nonblock(fd: int) -> None:
    """Clear O_NONBLOCK on ``fd`` (no-op semantics for regular files, but correct)."""
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    if flags & os.O_NONBLOCK:
        fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)


def open_regular_fd(real_path: Path, *, follow_symlinks: bool) -> int:
    """Open ``real_path`` read-only and prove it is a regular file.

    Opens with ``O_RDONLY | O_NONBLOCK | O_CLOEXEC`` plus ``O_NOFOLLOW`` when
    ``follow_symlinks`` is False. ``O_NONBLOCK`` is CRITICAL: it prevents
    ``os.open()`` from blocking FOREVER on a FIFO that has no writer. After the
    open we ``fstat`` the fd and reject anything that is not a regular file via
    an ``S_ISREG`` whitelist, then clear ``O_NONBLOCK`` for clean blocking reads.

    Raises ``NasSecurityError("unsupported file type")`` for non-regular files
    (FIFO, socket, device, etc.). Lets ``FileNotFoundError`` / ``PermissionError``
    propagate to the manager for 404 / 403 mapping.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
    if not follow_symlinks:
        flags |= os.O_NOFOLLOW

    try:
        fd = os.open(real_path, flags)
    except OSError as exc:
        # Some special files fail at open() before fstat can classify them:
        # a UNIX-domain socket raises ENXIO, certain devices ENODEV/EOPNOTSUPP.
        # Map those to the same "unsupported file type" rejection as the
        # post-open S_ISREG whitelist. FileNotFoundError (ENOENT) and
        # PermissionError (EACCES) are distinct OSError subclasses and are
        # re-raised unchanged for the manager's 404 / 403 mapping.
        if exc.errno in (errno.ENXIO, errno.ENODEV, errno.EOPNOTSUPP):
            logger.debug("open_regular_fd: special file rejected at open (errno=%s)", exc.errno)
            raise NasSecurityError("unsupported file type") from exc
        raise
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise

    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        logger.debug("open_regular_fd: non-regular file rejected (mode=%o)", st.st_mode)
        raise NasSecurityError("unsupported file type")

    # No-op for regular files, but keeps the fd in the expected blocking state.
    _clear_nonblock(fd)
    return fd


# --------------------------------------------------------------------------- #
# Directory-entry classification
# --------------------------------------------------------------------------- #
def classify_dirent(entry: os.DirEntry, *, show_hidden: bool, follow_symlinks: bool) -> bool:
    """Decide whether a scandir entry should appear in a listing.

    Returns True to include the entry. Drops:
      - dotfiles / OS-junk (``.DS_Store``, ``Thumbs.db``, ``$RECYCLE.BIN``,
        ``.git``) when ``show_hidden`` is False;
      - symlinks when ``follow_symlinks`` is False;
      - FIFO / socket / char / block devices ALWAYS (only regular files and
        directories are whitelisted);
      - entries whose name fails a utf-8 surrogateescape round-trip.
    """
    name = entry.name

    if not _name_is_utf8_clean(name):
        return False

    if not show_hidden and _is_hidden_name(name):
        return False

    # Detect symlinks without following them.
    try:
        is_link = entry.is_symlink()
    except OSError:
        return False
    if is_link and not follow_symlinks:
        return False

    # Whitelist only regular files and directories. follow_symlinks controls
    # whether we resolve a symlink target's type or treat the link itself.
    try:
        if entry.is_dir(follow_symlinks=follow_symlinks):
            return True
        if entry.is_file(follow_symlinks=follow_symlinks):
            return True
    except OSError:
        return False

    # FIFO / socket / device / broken symlink -> excluded.
    return False
