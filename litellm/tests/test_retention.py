"""Tests for the dataset-retention logic in ``litellm/custom_callbacks.py`` (#44).

``custom_callbacks.py`` imports ``litellm`` at module load, which is not
installed in this dev/CI environment. So before importing the module under test
we install a minimal stand-in for ``litellm.integrations.custom_logger`` and
then load the file by path — exercising the *real* retention code, not a copy.

Run from the repo root: ``python -m pytest litellm/tests/``.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import types
from datetime import datetime, timezone

# --- Make custom_callbacks importable without the real litellm dependency. ----
try:  # pragma: no cover - depends on the environment
    import litellm.integrations.custom_logger  # noqa: F401
except Exception:
    _litellm = sys.modules.setdefault("litellm", types.ModuleType("litellm"))
    _integ = sys.modules.setdefault(
        "litellm.integrations", types.ModuleType("litellm.integrations")
    )
    _cl = types.ModuleType("litellm.integrations.custom_logger")

    class CustomLogger:  # minimal base: the module only subclasses it
        def __init__(self, *args, **kwargs):
            pass

    _cl.CustomLogger = CustomLogger
    _litellm.integrations = _integ
    _integ.custom_logger = _cl
    sys.modules["litellm.integrations.custom_logger"] = _cl

# The module instantiates a logger at import; point its dir at a throwaway temp
# location so import never touches the default /var/lib path.
import os  # noqa: E402

os.environ["LITELLM_DATASET_DIR"] = tempfile.mkdtemp(prefix="litellm-dataset-import-")

_MODULE_PATH = pathlib.Path(__file__).resolve().parent.parent / "custom_callbacks.py"
_spec = importlib.util.spec_from_file_location("custom_callbacks_under_test", _MODULE_PATH)
cc = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(cc)


# --- Helpers -----------------------------------------------------------------
def make_logger(tmp_path, monkeypatch, *, retention_days=0, max_total_bytes=0):
    monkeypatch.setenv("LITELLM_DATASET_DIR", str(tmp_path))
    monkeypatch.setenv("LITELLM_DATASET_RETENTION_DAYS", str(retention_days))
    monkeypatch.setenv("LITELLM_DATASET_MAX_TOTAL_BYTES", str(max_total_bytes))
    return cc.JsonlDatasetLogger()


def write_dataset_file(dir_path, date_key, size=10):
    p = pathlib.Path(dir_path) / f"dataset-{date_key}.jsonl"
    p.write_bytes(b"x" * size)
    return p


def names(tmp_path):
    return sorted(p.name for p in pathlib.Path(tmp_path).iterdir())


# --- Pure helpers ------------------------------------------------------------
def test_env_int_parsing(monkeypatch):
    monkeypatch.delenv("X_UNSET", raising=False)
    assert cc._env_int("X_UNSET", 0) == 0
    monkeypatch.setenv("X_EMPTY", "")
    assert cc._env_int("X_EMPTY", 3) == 3
    monkeypatch.setenv("X_NEG", "-5")
    assert cc._env_int("X_NEG", 9) == 0  # negative collapses to the disabled sentinel
    monkeypatch.setenv("X_BAD", "abc")
    assert cc._env_int("X_BAD", 7) == 7  # unparseable falls back to default
    monkeypatch.setenv("X_OK", "12")
    assert cc._env_int("X_OK", 0) == 12


def test_dataset_file_date():
    assert cc._dataset_file_date("dataset-20260826.jsonl") == "20260826"
    assert cc._dataset_file_date("dataset-2026082.jsonl") is None  # not 8 digits
    assert cc._dataset_file_date("dataset-20261332.jsonl") is None  # month 13
    assert cc._dataset_file_date("dataset-20260826.txt") is None  # wrong suffix
    assert cc._dataset_file_date("other-20260826.jsonl") is None  # wrong prefix
    assert cc._dataset_file_date("dataset-.jsonl") is None
    assert cc._dataset_file_date("dataset-2026082x.jsonl") is None  # non-digit


# --- Retention by age --------------------------------------------------------
def test_retention_days_deletes_old_keeps_recent(tmp_path, monkeypatch):
    logger = make_logger(tmp_path, monkeypatch, retention_days=7)
    for key in ("20260816", "20260817", "20260818", "20260819", "20260820", "20260826"):
        write_dataset_file(tmp_path, key)
    logger._run_cleanup("20260826")  # keep 7 days -> cutoff 20260820
    assert names(tmp_path) == ["dataset-20260820.jsonl", "dataset-20260826.jsonl"]


def test_retention_one_day_keeps_only_today(tmp_path, monkeypatch):
    logger = make_logger(tmp_path, monkeypatch, retention_days=1)
    write_dataset_file(tmp_path, "20260825")
    write_dataset_file(tmp_path, "20260826")
    logger._run_cleanup("20260826")
    assert names(tmp_path) == ["dataset-20260826.jsonl"]


# --- Retention by total size -------------------------------------------------
def test_max_total_bytes_deletes_oldest_first(tmp_path, monkeypatch):
    logger = make_logger(tmp_path, monkeypatch, max_total_bytes=250)
    write_dataset_file(tmp_path, "20260820", 100)
    write_dataset_file(tmp_path, "20260821", 100)
    write_dataset_file(tmp_path, "20260822", 100)
    write_dataset_file(tmp_path, "20260826", 100)  # today
    logger._run_cleanup("20260826")  # 400 -> drop 0820, 0821 -> 200 <= 250
    assert names(tmp_path) == ["dataset-20260822.jsonl", "dataset-20260826.jsonl"]


def test_today_file_never_deleted_by_byte_cap(tmp_path, monkeypatch):
    logger = make_logger(tmp_path, monkeypatch, max_total_bytes=50)
    write_dataset_file(tmp_path, "20260826", 100)  # today alone exceeds the cap
    logger._run_cleanup("20260826")
    assert (tmp_path / "dataset-20260826.jsonl").exists()


def test_days_and_bytes_combined(tmp_path, monkeypatch):
    logger = make_logger(tmp_path, monkeypatch, retention_days=3, max_total_bytes=150)
    write_dataset_file(tmp_path, "20260820", 100)  # aged out (cutoff 20260824)
    write_dataset_file(tmp_path, "20260824", 100)  # survives age, trimmed by bytes
    write_dataset_file(tmp_path, "20260825", 100)  # survives age, trimmed by bytes
    write_dataset_file(tmp_path, "20260826", 100)  # today, protected
    logger._run_cleanup("20260826")
    assert names(tmp_path) == ["dataset-20260826.jsonl"]


def test_ignores_unrelated_files(tmp_path, monkeypatch):
    logger = make_logger(tmp_path, monkeypatch, retention_days=1)
    (tmp_path / "notes.txt").write_text("keep me")
    (tmp_path / "dataset-bad.jsonl").write_text("not a date")
    write_dataset_file(tmp_path, "20260101")  # aged out
    write_dataset_file(tmp_path, "20260826")  # today
    logger._run_cleanup("20260826")
    remaining = names(tmp_path)
    assert "notes.txt" in remaining
    assert "dataset-bad.jsonl" in remaining
    assert "dataset-20260826.jsonl" in remaining
    assert "dataset-20260101.jsonl" not in remaining


# --- Throttling / never-raise (via _maybe_cleanup) ---------------------------
def test_disabled_is_noop(tmp_path, monkeypatch):
    logger = make_logger(tmp_path, monkeypatch)  # both caps 0 = disabled
    write_dataset_file(tmp_path, "20200101")  # ancient
    logger._maybe_cleanup(datetime(2026, 8, 26, tzinfo=timezone.utc))
    assert (tmp_path / "dataset-20200101.jsonl").exists()
    assert logger._last_cleanup_date == ""  # returned before doing any work


def test_cleanup_never_raises_on_missing_dir(tmp_path, monkeypatch):
    logger = make_logger(tmp_path, monkeypatch, retention_days=1)
    logger.dataset_dir = str(tmp_path / "does-not-exist")
    logger._maybe_cleanup(datetime(2026, 8, 26, tzinfo=timezone.utc))  # must not raise


def test_throttle_runs_once_per_day(tmp_path, monkeypatch):
    logger = make_logger(tmp_path, monkeypatch, retention_days=1)
    calls: list[str] = []
    monkeypatch.setattr(logger, "_run_cleanup", lambda today: calls.append(today))
    d1 = datetime(2026, 8, 26, tzinfo=timezone.utc)
    logger._maybe_cleanup(d1)
    logger._maybe_cleanup(d1)  # same day, no byte cap -> not due
    assert calls == ["20260826"]
    logger._maybe_cleanup(datetime(2026, 8, 27, tzinfo=timezone.utc))
    assert calls == ["20260826", "20260827"]


def test_byte_cap_time_throttle_allows_same_day_rerun(tmp_path, monkeypatch):
    logger = make_logger(tmp_path, monkeypatch, max_total_bytes=100)
    calls: list[str] = []
    monkeypatch.setattr(logger, "_run_cleanup", lambda today: calls.append(today))
    d1 = datetime(2026, 8, 26, tzinfo=timezone.utc)
    logger._maybe_cleanup(d1)  # due: first run of the day
    logger._maybe_cleanup(d1)  # within the interval -> not due
    assert calls == ["20260826"]
    logger._last_cleanup_at -= cc.SIZE_CHECK_INTERVAL_SECONDS + 1  # interval elapsed
    logger._maybe_cleanup(d1)  # due again via the byte-cap time throttle
    assert calls == ["20260826", "20260826"]
