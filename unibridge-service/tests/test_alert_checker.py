"""Tests for alert_checker module."""
from __future__ import annotations

from types import SimpleNamespace
import time

import pytest
from unittest.mock import AsyncMock, patch

from app.services.alert_checker import run_single_check
from app.services.alert_state import AlertStateManager


class _FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self.rows)

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class _FailingCommitDb:
    def __init__(self):
        self._results = [
            _FakeResult([SimpleNamespace(id=7, name="critical rule")]),
            _FakeResult([SimpleNamespace(channel_id=3, recipients='["ops@example.com"]')]),
            _FakeResult([
                SimpleNamespace(
                    id=3,
                    enabled=True,
                    webhook_url="http://hook.example.com/alerts",
                    payload_template='{"text":"{{message}}"}',
                    headers=None,
                )
            ]),
        ]

    async def execute(self, _query):
        return self._results.pop(0)

    def add(self, _entry):
        return None

    async def commit(self):
        raise RuntimeError("history commit failed")


class _CommitCountingDb:
    def __init__(self):
        self.commit_count = 0
        self._results = [
            _FakeResult([
                SimpleNamespace(id=7, name="critical rule"),
                SimpleNamespace(id=8, name="secondary rule"),
            ]),
            _FakeResult([SimpleNamespace(channel_id=3, recipients='["ops@example.com"]')]),
            _FakeResult([
                SimpleNamespace(
                    id=3,
                    enabled=True,
                    webhook_url="http://hook.example.com/alerts-a",
                    payload_template='{"text":"{{message}}"}',
                    headers=None,
                )
            ]),
            _FakeResult([SimpleNamespace(channel_id=4, recipients='["dev@example.com"]')]),
            _FakeResult([
                SimpleNamespace(
                    id=4,
                    enabled=True,
                    webhook_url="http://hook.example.com/alerts-b",
                    payload_template='{"text":"{{message}}"}',
                    headers=None,
                )
            ]),
        ]

    async def execute(self, _query):
        return self._results.pop(0)

    def add(self, _entry):
        return None

    async def commit(self):
        self.commit_count += 1


class _FakeSessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TestAlertChecker:
    @pytest.mark.asyncio
    async def test_db_health_triggered(self):
        state = AlertStateManager()
        state.update("db_health", "mydb", is_healthy=True)

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_error_rate", new_callable=AsyncMock) as mock_err, \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker._dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = [("mydb", False)]
            mock_up.return_value = []
            mock_err.return_value = []

            await run_single_check(state)

            assert state.get_status("db_health", "mydb") == "alert"
            mock_dispatch.assert_called_once()
            call_args = mock_dispatch.call_args
            assert call_args[1]["alert_type"] == "triggered"
            assert call_args[1]["target"] == "mydb"

    @pytest.mark.asyncio
    async def test_db_health_resolved(self):
        state = AlertStateManager()
        state.update("db_health", "mydb", is_healthy=False)
        state.update("db_health", "mydb", is_healthy=False)

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_error_rate", new_callable=AsyncMock) as mock_err, \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker._dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = [("mydb", True)]
            mock_up.return_value = []
            mock_err.return_value = []

            await run_single_check(state)

            assert state.get_status("db_health", "mydb") == "ok"
            mock_dispatch.assert_called_once()
            assert mock_dispatch.call_args[1]["alert_type"] == "resolved"

    @pytest.mark.asyncio
    async def test_no_dispatch_when_no_transition(self):
        state = AlertStateManager()

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_error_rate", new_callable=AsyncMock) as mock_err, \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker._dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = [("mydb", True)]
            mock_up.return_value = []
            mock_err.return_value = []

            await run_single_check(state)

            mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_upstream_health_triggered(self):
        state = AlertStateManager()
        state.update("upstream_health", "order-svc", is_healthy=True)

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_error_rate", new_callable=AsyncMock) as mock_err, \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker._dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = []
            mock_up.return_value = [("order-svc", False)]
            mock_err.return_value = []

            await run_single_check(state)

            assert state.get_status("upstream_health", "order-svc") == "alert"
            mock_dispatch.assert_called_once()
            assert mock_dispatch.call_args[1]["rule_type"] == "upstream_health"

    @pytest.mark.asyncio
    async def test_initial_unhealthy_db_cycle_is_silent_then_dispatches_if_still_down(self):
        state = AlertStateManager()

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_error_rate", new_callable=AsyncMock) as mock_err, \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker._dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = [("boot-db", False)]
            mock_up.return_value = []
            mock_err.return_value = []

            await run_single_check(state)
            mock_dispatch.assert_not_called()
            assert state.get_status("db_health", "boot-db") == "alert"

            await run_single_check(state)
            mock_dispatch.assert_called_once()
            assert mock_dispatch.call_args[1]["alert_type"] == "triggered"
            assert mock_dispatch.call_args[1]["target"] == "boot-db"

    @pytest.mark.asyncio
    async def test_initial_unhealthy_db_recovery_does_not_send_resolved_without_trigger(self):
        state = AlertStateManager()

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_error_rate", new_callable=AsyncMock) as mock_err, \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker._dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.side_effect = [[("boot-db", False)], [("boot-db", True)]]
            mock_up.return_value = []
            mock_err.return_value = []

            await run_single_check(state)
            await run_single_check(state)

        mock_dispatch.assert_not_called()
        assert state.get_status("db_health", "boot-db") == "ok"

    @pytest.mark.asyncio
    async def test_start_checker_schedules_from_cycle_start_to_avoid_drift(self):
        from app.services import alert_checker

        state = AlertStateManager()
        sleep_delays: list[float] = []

        async def stop_after_sleep(delay: float):
            sleep_delays.append(delay)
            raise RuntimeError("stop loop")

        with patch("app.services.alert_checker.run_single_check", new_callable=AsyncMock), \
             patch("app.services.alert_checker._monotonic", side_effect=[100.0, 115.0]), \
             patch("app.services.alert_checker.asyncio.sleep", new=AsyncMock(side_effect=stop_after_sleep)):
            task = await alert_checker.start_checker(state)
            with pytest.raises(RuntimeError, match="stop loop"):
                await task

        assert sleep_delays == [45.0]


class TestDispatchAlertMetrics:
    @pytest.mark.asyncio
    async def test_dispatch_metric_not_recorded_when_history_commit_fails(self):
        from app.services.alert_checker import _dispatch_alert

        fake_db = _FailingCommitDb()

        with patch("app.services.alert_checker.async_session", return_value=_FakeSessionContext(fake_db)), \
             patch("app.services.alert_checker.send_webhook", new=AsyncMock(return_value=(True, None))), \
             patch("app.services.alert_checker.metrics.record_alert_dispatch") as record_metric:
            with pytest.raises(RuntimeError, match="history commit failed"):
                await _dispatch_alert(
                    rule_type="db_health",
                    alert_type="triggered",
                    target="meta",
                    message="metadata db down",
                )

        record_metric.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_commits_once_after_all_matching_rules(self):
        from app.services.alert_checker import _dispatch_alert

        fake_db = _CommitCountingDb()

        with patch("app.services.alert_checker.async_session", return_value=_FakeSessionContext(fake_db)), \
             patch("app.services.alert_checker.send_webhook", new=AsyncMock(return_value=(True, None))), \
             patch("app.services.alert_checker.metrics.record_alert_dispatch"):
            await _dispatch_alert(
                rule_type="db_health",
                alert_type="triggered",
                target="meta",
                message="metadata db down",
            )

        assert fake_db.commit_count == 1


class TestCheckRouteErrorRate:
    @pytest.mark.asyncio
    async def test_computes_rate_per_route_with_resolved_zero(self):
        """Routes with traffic but no 5xx should yield rate=0 (not disappear)."""
        from app.services.alert_checker import _check_route_error_rate

        async def mock_query(query):
            if "code=~" in query:
                # Only r1 has 5xx errors
                return [{"metric": {"route": "r1"}, "value": [0, "2.0"]}]
            # total traffic for all three routes
            return [
                {"metric": {"route": "r1"}, "value": [0, "20.0"]},
                {"metric": {"route": "r2"}, "value": [0, "10.0"]},
                {"metric": {"route": "r3"}, "value": [0, "0"]},  # no traffic → skipped
            ]

        with patch(
            "app.services.prometheus_client.instant_query",
            new=AsyncMock(side_effect=mock_query),
        ):
            results = await _check_route_error_rate()

        d = dict(results)
        assert d["r1"] == pytest.approx(10.0)   # 2/20 = 10%
        assert d["r2"] == pytest.approx(0.0)    # 0/10 = 0% (resolvable)
        assert "r3" not in d                     # zero-traffic skipped

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_traffic(self):
        from app.services.alert_checker import _check_route_error_rate
        with patch(
            "app.services.prometheus_client.instant_query",
            new=AsyncMock(return_value=[]),
        ):
            results = await _check_route_error_rate()
        assert results == []

    @pytest.mark.asyncio
    async def test_prometheus_failure_returns_empty(self):
        from app.services.alert_checker import _check_route_error_rate
        with patch(
            "app.services.prometheus_client.instant_query",
            new=AsyncMock(side_effect=RuntimeError("prom down")),
        ):
            results = await _check_route_error_rate()
        assert results == []


class TestRouteLabelCache:
    @pytest.mark.asyncio
    async def test_label_falls_back_to_id_on_miss(self):
        from app.services import alert_checker
        # Force fresh cache with one known mapping
        alert_checker._ROUTE_LABEL_CACHE = {"r1": "login-api"}
        alert_checker._ROUTE_LABEL_CACHE_TS = 9e18  # far future — skip refresh
        try:
            assert await alert_checker._get_route_label("r1") == "login-api"
            assert await alert_checker._get_route_label("unknown") == "unknown"
        finally:
            alert_checker._ROUTE_LABEL_CACHE = {}
            alert_checker._ROUTE_LABEL_CACHE_TS = 0.0

    @pytest.mark.asyncio
    async def test_refresh_prefers_name_then_uri_then_id(self):
        from app.services import alert_checker
        alert_checker._ROUTE_LABEL_CACHE = {}
        alert_checker._ROUTE_LABEL_CACHE_TS = 0.0
        fake = {"items": [
            {"id": "r-with-name", "name": "Login API", "uri": "/login"},
            {"id": "r-uri-only", "uri": "/orders"},
            {"id": "r-bare"},
        ]}
        with patch(
            "app.services.apisix_client.list_resources",
            new=AsyncMock(return_value=fake),
        ):
            await alert_checker._refresh_route_labels()
        try:
            assert alert_checker._ROUTE_LABEL_CACHE["r-with-name"] == "Login API"
            assert alert_checker._ROUTE_LABEL_CACHE["r-uri-only"] == "/orders"
            assert alert_checker._ROUTE_LABEL_CACHE["r-bare"] == "r-bare"
        finally:
            alert_checker._ROUTE_LABEL_CACHE = {}
            alert_checker._ROUTE_LABEL_CACHE_TS = 0.0

    @pytest.mark.asyncio
    async def test_refresh_failure_still_advances_ts(self):
        """APISIX outage must not cause per-call refresh storm:
        TS updates in finally block so TTL governs retry cadence."""
        from app.services import alert_checker
        alert_checker._ROUTE_LABEL_CACHE = {}
        alert_checker._ROUTE_LABEL_CACHE_TS = 0.0
        with patch(
            "app.services.apisix_client.list_resources",
            new=AsyncMock(side_effect=RuntimeError("apisix down")),
        ):
            await alert_checker._refresh_route_labels()
        try:
            assert alert_checker._ROUTE_LABEL_CACHE_TS > 0.0
            assert alert_checker._ROUTE_LABEL_CACHE == {}
        finally:
            alert_checker._ROUTE_LABEL_CACHE = {}
            alert_checker._ROUTE_LABEL_CACHE_TS = 0.0

    @pytest.mark.asyncio
    async def test_get_route_label_skips_refresh_within_ttl_after_failure(self):
        """After a failed refresh, subsequent calls within TTL must NOT
        re-fetch — otherwise N routes × M rules = N*M APISIX calls/cycle."""
        from app.services import alert_checker
        alert_checker._ROUTE_LABEL_CACHE = {}
        # Force the cache to look expired. Setting TS to 0 only works when the
        # process's monotonic clock is already > TTL, which is not guaranteed
        # on freshly-booted CI runners.
        alert_checker._ROUTE_LABEL_CACHE_TS = (
            time.monotonic() - alert_checker._ROUTE_LABEL_TTL - 10.0
        )
        call_count = {"n": 0}

        async def failing(*a, **kw):
            call_count["n"] += 1
            raise RuntimeError("apisix down")

        with patch(
            "app.services.apisix_client.list_resources",
            new=AsyncMock(side_effect=failing),
        ):
            await alert_checker._get_route_label("r1")
            await alert_checker._get_route_label("r2")
            await alert_checker._get_route_label("r3")
        try:
            assert call_count["n"] == 1  # not 3
        finally:
            alert_checker._ROUTE_LABEL_CACHE = {}
            alert_checker._ROUTE_LABEL_CACHE_TS = 0.0
