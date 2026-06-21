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

    def one_or_none(self):
        return self.rows[0] if self.rows else None


class _FakeSessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _threshold_db(threshold: float = 10.0, min_requests: int = 0):
    """A fake session whose route-settings query returns (threshold, min_requests).

    Defaults to min_requests=0 so the low-traffic floor is disabled and tests
    exercise the threshold logic directly.
    """
    return SimpleNamespace(
        execute=AsyncMock(return_value=_FakeResult([(threshold, min_requests)]))
    )


class TestAlertChecker:
    @pytest.mark.asyncio
    async def test_db_health_triggered(self):
        state = AlertStateManager()
        # Seed fail_count=1 so the next unhealthy observation crosses N=2.
        state.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = [("mydb", False)]
            mock_up.return_value = []

            await run_single_check(state, trigger_after_failures=2)

            assert state.get_status("db_health", "mydb") == "alert"
            mock_dispatch.assert_called_once()
            kwargs = mock_dispatch.call_args.kwargs
            assert kwargs["resource_type"] == "db"
            assert kwargs["resource_id"] == "mydb"
            assert kwargs["alert_type"] == "triggered"
            assert kwargs["target"] == "mydb"
            assert kwargs["message"] == "Database 'mydb' connection failed."
            assert kwargs["display_target"] == "mydb"

    @pytest.mark.asyncio
    async def test_db_health_resolved(self):
        state = AlertStateManager()
        state.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        state.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = [("mydb", True)]
            mock_up.return_value = []

            await run_single_check(state, trigger_after_failures=2)

            assert state.get_status("db_health", "mydb") == "ok"
            mock_dispatch.assert_called_once()
            kwargs = mock_dispatch.call_args.kwargs
            assert kwargs["resource_type"] == "db"
            assert kwargs["resource_id"] == "mydb"
            assert kwargs["alert_type"] == "resolved"
            assert kwargs["target"] == "mydb"
            assert kwargs["message"] == "Database 'mydb' connection restored."
            assert kwargs["display_target"] == "mydb"

    @pytest.mark.asyncio
    async def test_nas_health_triggered(self):
        state = AlertStateManager()
        # Seed fail_count=1 so the next unhealthy observation crosses N=2.
        state.update("nas_health", "reports-nas", is_healthy=False, trigger_after_failures=2)

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_nas_health", new_callable=AsyncMock) as mock_nas, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = []
            mock_nas.return_value = [("reports-nas", False)]
            mock_up.return_value = []

            await run_single_check(state, trigger_after_failures=2)

            assert state.get_status("nas_health", "reports-nas") == "alert"
            mock_dispatch.assert_called_once()
            kwargs = mock_dispatch.call_args.kwargs
            assert kwargs["resource_type"] == "nas"
            assert kwargs["resource_id"] == "reports-nas"
            assert kwargs["alert_type"] == "triggered"
            assert kwargs["target"] == "reports-nas"
            assert kwargs["message"] == "NAS connection 'reports-nas' is unavailable."
            assert kwargs["display_target"] == "reports-nas"

    @pytest.mark.asyncio
    async def test_no_dispatch_when_no_transition(self):
        state = AlertStateManager()

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = [("mydb", True)]
            mock_up.return_value = []

            await run_single_check(state, trigger_after_failures=2)

            mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_upstream_health_triggered(self):
        state = AlertStateManager()
        # Seed fail_count=1 so the next unhealthy observation crosses N=2.
        state.update("upstream_health", "order-svc", is_healthy=False, trigger_after_failures=2)

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = []
            mock_up.return_value = [("order-svc", False)]

            await run_single_check(state, trigger_after_failures=2)

            assert state.get_status("upstream_health", "order-svc") == "alert"
            mock_dispatch.assert_called_once()
            kwargs = mock_dispatch.call_args.kwargs
            assert kwargs["resource_type"] == "upstream"
            assert kwargs["resource_id"] == "order-svc"
            assert kwargs["alert_type"] == "triggered"
            assert kwargs["target"] == "order-svc"
            assert kwargs["message"] == "Upstream 'order-svc' is down."
            assert kwargs["display_target"] == "order-svc"

    @pytest.mark.asyncio
    async def test_upstream_health_dispatch_includes_name_in_display(self):
        from app.services import alert_checker

        state = AlertStateManager()
        # Seed fail_count=1 so the next unhealthy observation crosses N=2.
        state.update("upstream_health", "upstream-1", is_healthy=False, trigger_after_failures=2)
        alert_checker._UPSTREAM_NAME_BY_ID = {"upstream-1": "payments-api"}

        try:
            with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
                 patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
                 patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
                 patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
                mock_db.return_value = []
                mock_up.return_value = [("upstream-1", False)]

                await run_single_check(state, trigger_after_failures=2)

                mock_dispatch.assert_called_once()
                kwargs = mock_dispatch.call_args.kwargs
                assert kwargs["resource_type"] == "upstream"
                assert kwargs["resource_id"] == "upstream-1"
                assert kwargs["target"] == "upstream-1"
                assert kwargs["display_target"] == "payments-api (upstream-1)"
        finally:
            alert_checker._UPSTREAM_NAME_BY_ID = {}

    @pytest.mark.asyncio
    async def test_initial_unhealthy_db_cycle_is_silent_then_dispatches_if_still_down(self):
        state = AlertStateManager()

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = [("boot-db", False)]
            mock_up.return_value = []

            await run_single_check(state, trigger_after_failures=2)
            mock_dispatch.assert_not_called()
            assert state.get_status("db_health", "boot-db") == "ok"

            await run_single_check(state, trigger_after_failures=2)
            mock_dispatch.assert_called_once()
            assert mock_dispatch.call_args.kwargs["alert_type"] == "triggered"
            assert mock_dispatch.call_args.kwargs["target"] == "boot-db"
            assert state.get_status("db_health", "boot-db") == "alert"

    @pytest.mark.asyncio
    async def test_initial_unhealthy_db_recovery_does_not_send_resolved_without_trigger(self):
        state = AlertStateManager()

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.side_effect = [[("boot-db", False)], [("boot-db", True)]]
            mock_up.return_value = []

            await run_single_check(state, trigger_after_failures=2)
            await run_single_check(state, trigger_after_failures=2)

        mock_dispatch.assert_not_called()
        assert state.get_status("db_health", "boot-db") == "ok"

    @pytest.mark.asyncio
    async def test_route_error_rate_dispatches_alert_with_route_context(self):
        state = AlertStateManager()
        # Seed fail_count=1 (state keyed by the plain route_id) so the next
        # unhealthy observation crosses N=2.
        state.update(
            "route_error_rate",
            "route-a",
            is_healthy=False,
            display_target="checkout (route-a)",
            trigger_after_failures=2,
        )

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_route_error_rate", new=AsyncMock(return_value=[("route-a", 12.5, 100.0)])), \
             patch("app.services.alert_checker.async_session", return_value=_FakeSessionContext(_threshold_db(10.0))), \
             patch("app.services.alert_checker._get_route_label", new=AsyncMock(return_value="checkout")), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = []
            mock_up.return_value = []

            await run_single_check(state, trigger_after_failures=2)

        mock_dispatch.assert_called_once()
        kwargs = mock_dispatch.call_args.kwargs
        assert kwargs["resource_type"] == "route"
        assert kwargs["resource_id"] == "route-a"
        assert kwargs["alert_type"] == "triggered"
        assert kwargs["target"] == "route-a"
        assert kwargs["message"] == (
            "Route 'checkout (route-a)' 5xx error rate is 12.5% (threshold: 10.0%)."
        )
        assert kwargs["display_target"] == "checkout (route-a)"
        assert kwargs["rate"] == 12.5
        assert kwargs["threshold"] == 10.0
        assert kwargs["monitor_label"] == "라우트 에러율"

    @pytest.mark.asyncio
    async def test_route_error_rate_uses_settings_threshold(self):
        state = AlertStateManager()
        # Seed fail_count=1 so the next unhealthy observation crosses N=2.
        state.update(
            "route_error_rate",
            "route-a",
            is_healthy=False,
            display_target="checkout (route-a)",
            trigger_after_failures=2,
        )

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_route_error_rate", new=AsyncMock(return_value=[("route-a", 4.0, 100.0)])), \
             patch("app.services.alert_checker.async_session", return_value=_FakeSessionContext(_threshold_db(3.0))), \
             patch("app.services.alert_checker._get_route_label", new=AsyncMock(return_value="checkout")), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = []
            mock_up.return_value = []

            await run_single_check(state, trigger_after_failures=2)

        mock_dispatch.assert_called_once()
        kwargs = mock_dispatch.call_args.kwargs
        assert kwargs["threshold"] == 3.0
        assert "threshold: 3.0%" in kwargs["message"]

    @pytest.mark.asyncio
    async def test_route_error_rate_below_threshold_does_not_dispatch(self):
        state = AlertStateManager()
        # Seed fail_count=1; but a healthy (below-threshold) reading resets it.
        state.update(
            "route_error_rate",
            "route-a",
            is_healthy=False,
            display_target="checkout (route-a)",
            trigger_after_failures=2,
        )

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_route_error_rate", new=AsyncMock(return_value=[("route-a", 4.0, 100.0)])), \
             patch("app.services.alert_checker.async_session", return_value=_FakeSessionContext(_threshold_db(10.0))), \
             patch("app.services.alert_checker._get_route_label", new=AsyncMock(return_value="checkout")), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = []
            mock_up.return_value = []

            await run_single_check(state, trigger_after_failures=2)

        mock_dispatch.assert_not_called()
        assert state.get_status("route_error_rate", "route-a") == "ok"

    @pytest.mark.asyncio
    async def test_route_error_rate_below_min_requests_does_not_dispatch(self):
        """A high error rate on a low-traffic route must not trigger an alert."""
        state = AlertStateManager()
        # Seed fail_count=1 so a non-guarded unhealthy reading would cross N=2.
        state.update(
            "route_error_rate",
            "route-a",
            is_healthy=False,
            display_target="checkout (route-a)",
            trigger_after_failures=2,
        )

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_route_error_rate", new=AsyncMock(return_value=[("route-a", 50.0, 5.0)])), \
             patch("app.services.alert_checker.async_session", return_value=_FakeSessionContext(_threshold_db(10.0, min_requests=20))), \
             patch("app.services.alert_checker._get_route_label", new=AsyncMock(return_value="checkout")), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = []
            mock_up.return_value = []

            await run_single_check(state, trigger_after_failures=2)

        # 50% error rate but only 5 requests (< 20 floor) → treated as healthy.
        mock_dispatch.assert_not_called()
        assert state.get_status("route_error_rate", "route-a") == "ok"

    @pytest.mark.asyncio
    async def test_start_checker_schedules_from_cycle_start_to_avoid_drift(self):
        from app.services import alert_checker

        state = AlertStateManager()
        sleep_delays: list[float] = []

        async def stop_after_sleep(delay: float):
            sleep_delays.append(delay)
            raise RuntimeError("stop loop")

        with patch("app.services.alert_checker.run_single_check", new_callable=AsyncMock), \
             patch("app.services.alert_checker._get_check_interval_seconds", new=AsyncMock(return_value=60)), \
             patch("app.services.alert_checker._get_trigger_after_failures", new=AsyncMock(return_value=2)), \
             patch("app.services.alert_checker._monotonic", side_effect=[100.0, 115.0]), \
             patch("app.services.alert_checker.asyncio.sleep", new=AsyncMock(side_effect=stop_after_sleep)):
            task = await alert_checker.start_checker(state)
            with pytest.raises(RuntimeError, match="stop loop"):
                await task

        assert sleep_delays == [45.0]

    @pytest.mark.asyncio
    async def test_start_checker_uses_configured_check_interval(self):
        from app.services import alert_checker

        state = AlertStateManager()
        sleep_delays: list[float] = []

        async def stop_after_sleep(delay: float):
            sleep_delays.append(delay)
            raise RuntimeError("stop loop")

        with patch("app.services.alert_checker.run_single_check", new_callable=AsyncMock), \
             patch("app.services.alert_checker._get_check_interval_seconds", new=AsyncMock(return_value=90)), \
             patch("app.services.alert_checker._get_trigger_after_failures", new=AsyncMock(return_value=2)), \
             patch("app.services.alert_checker._monotonic", side_effect=[100.0, 115.0]), \
             patch("app.services.alert_checker.asyncio.sleep", new=AsyncMock(side_effect=stop_after_sleep)):
            task = await alert_checker.start_checker(state)
            with pytest.raises(RuntimeError, match="stop loop"):
                await task

        assert sleep_delays == [75.0]

    @pytest.mark.asyncio
    async def test_get_check_interval_seconds_reads_alert_settings(self):
        from app.services import alert_checker

        fake_db = SimpleNamespace(execute=AsyncMock(return_value=_FakeResult([90])))

        with patch("app.services.alert_checker.async_session", return_value=_FakeSessionContext(fake_db)):
            assert await alert_checker._get_check_interval_seconds() == 90


class TestCheckRouteErrorRate:
    @pytest.mark.asyncio
    async def test_computes_rate_per_route_with_resolved_zero(self):
        """Routes with traffic but no 5xx should yield rate=0 (not disappear)."""
        from app.services.alert_checker import _check_route_error_rate

        async def mock_query(query):
            assert "increase(apisix_http_status" in query  # count-based, not rate()
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

        rates = {rid: pct for rid, pct, _count in results}
        counts = {rid: count for rid, _pct, count in results}
        assert rates["r1"] == pytest.approx(10.0)   # 2/20 = 10%
        assert rates["r2"] == pytest.approx(0.0)    # 0/10 = 0% (resolvable)
        assert "r3" not in rates                     # zero-traffic skipped
        assert counts["r1"] == pytest.approx(20.0)   # sample_count = request volume
        assert counts["r2"] == pytest.approx(10.0)

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
    async def test_prometheus_failure_returns_none(self):
        from app.services.alert_checker import _check_route_error_rate
        with patch(
            "app.services.prometheus_client.instant_query",
            new=AsyncMock(side_effect=RuntimeError("prom down")),
        ):
            results = await _check_route_error_rate()
        assert results is None

    @pytest.mark.asyncio
    async def test_route_error_rate_resolves_active_alert_when_route_has_no_traffic(self):
        state = AlertStateManager()
        # Drive the route (keyed by plain route_id) into an active alert.
        state.update(
            "route_error_rate",
            "route-a",
            is_healthy=True,
            display_target="checkout (route-a)",
            trigger_after_failures=2,
        )
        state.update(
            "route_error_rate",
            "route-a",
            is_healthy=False,
            display_target="checkout (route-a)",
            trigger_after_failures=2,
        )
        # With N=2, one unhealthy isn't enough — push a second so the entry
        # is actually in 'alert' status, the precondition for a resolution
        # dispatch on the next healthy observation.
        state.update(
            "route_error_rate",
            "route-a",
            is_healthy=False,
            display_target="checkout (route-a)",
            trigger_after_failures=2,
        )

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_route_error_rate", new=AsyncMock(return_value=[])), \
             patch("app.services.alert_checker.async_session", return_value=_FakeSessionContext(_threshold_db(5.0))), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = []
            mock_up.return_value = []

            await run_single_check(state, trigger_after_failures=2)

        mock_dispatch.assert_called_once()
        kwargs = mock_dispatch.call_args.kwargs
        assert kwargs["resource_type"] == "route"
        assert kwargs["resource_id"] == "route-a"
        assert kwargs["alert_type"] == "resolved"
        assert kwargs["target"] == "route-a"
        assert kwargs["rate"] == 0.0


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
        re-fetch — otherwise N routes = N APISIX calls/cycle."""
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


@pytest.mark.asyncio
async def test_run_single_check_respects_trigger_after_failures(monkeypatch, seeded_db):
    """With N=3, two consecutive unhealthy cycles must not dispatch; the third does."""
    from app.services import alert_checker
    from app.services.alert_state import AlertStateManager

    async def _async_return(value):
        return value

    monkeypatch.setattr(
        alert_checker,
        "_check_db_health",
        lambda: _async_return([("main-db", False)]),
    )
    monkeypatch.setattr(
        alert_checker, "_check_upstream_health", lambda: _async_return([]),
    )
    monkeypatch.setattr(
        alert_checker, "_check_route_error_rate", lambda: _async_return([]),
    )

    dispatched: list[str] = []

    async def fake_dispatch_alert(**kwargs):
        dispatched.append(kwargs["alert_type"])

    monkeypatch.setattr(
        alert_checker, "dispatch_alert", fake_dispatch_alert,
    )

    state = AlertStateManager()
    await alert_checker.run_single_check(state, trigger_after_failures=3)
    await alert_checker.run_single_check(state, trigger_after_failures=3)
    assert dispatched == []
    await alert_checker.run_single_check(state, trigger_after_failures=3)
    assert dispatched == ["triggered"]
