"""Integration test: audit log responses serialize timestamps as UTC-aware ISO."""

from __future__ import annotations

import re

import pytest

from tests.conftest import auth_header


UTC_ISO_SUFFIX = re.compile(r"(\+00:00|Z)$")


class TestAuditLogTimezone:
    async def test_audit_log_timestamp_is_utc_aware_iso(self, client, admin_token):
        resp = await client.get(
            "/admin/query/audit-logs",
            headers=auth_header(admin_token),
        )
        assert resp.status_code in (200, 204)
        data = resp.json()
        logs = data if isinstance(data, list) else data.get("items") or data.get("logs") or []
        if not logs:
            pytest.skip("no audit logs available to verify timestamp format")
        ts = logs[0].get("timestamp")
        assert ts is not None, "audit log entry is missing 'timestamp' field"
        assert UTC_ISO_SUFFIX.search(ts), (
            f"timestamp {ts!r} does not end with '+00:00' or 'Z' — "
            "Pydantic should serialize tz-aware datetime with UTC offset"
        )
