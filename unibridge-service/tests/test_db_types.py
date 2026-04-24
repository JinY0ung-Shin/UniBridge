"""Unit tests for UtcDateTime TypeDecorator."""

from datetime import datetime, timezone, timedelta

import pytest

from app.db_types import UtcDateTime


KST = timezone(timedelta(hours=9))


class TestUtcDateTimeBindParam:
    def test_none_passthrough(self):
        col = UtcDateTime()
        assert col.process_bind_param(None, None) is None

    def test_naive_treated_as_utc(self):
        col = UtcDateTime()
        naive = datetime(2026, 4, 22, 9, 0, 0)
        out = col.process_bind_param(naive, None)
        assert out.tzinfo is not None
        assert out.utcoffset() == timedelta(0)
        assert out.hour == 9

    def test_aware_normalized_to_utc(self):
        col = UtcDateTime()
        kst = datetime(2026, 4, 22, 18, 0, 0, tzinfo=KST)
        out = col.process_bind_param(kst, None)
        assert out.utcoffset() == timedelta(0)
        assert out.hour == 9  # 18 KST - 9 = 09 UTC

    def test_already_utc_unchanged(self):
        col = UtcDateTime()
        utc = datetime(2026, 4, 22, 9, 0, 0, tzinfo=timezone.utc)
        out = col.process_bind_param(utc, None)
        assert out == utc


class TestUtcDateTimeResultValue:
    def test_none_passthrough(self):
        col = UtcDateTime()
        assert col.process_result_value(None, None) is None

    def test_naive_from_db_tagged_utc(self):
        col = UtcDateTime()
        naive = datetime(2026, 4, 22, 9, 0, 0)  # legacy naive row
        out = col.process_result_value(naive, None)
        assert out.tzinfo is not None
        assert out.utcoffset() == timedelta(0)
        assert out.hour == 9

    def test_aware_from_db_normalized_to_utc(self):
        col = UtcDateTime()
        kst = datetime(2026, 4, 22, 18, 0, 0, tzinfo=KST)
        out = col.process_result_value(kst, None)
        assert out.utcoffset() == timedelta(0)
        assert out.hour == 9
