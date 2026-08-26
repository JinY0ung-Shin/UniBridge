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
            mock_db.return_value = [("mydb", False, None)]
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
            mock_db.return_value = [("mydb", True, None)]
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
            mock_nas.return_value = [("reports-nas", False, None)]
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
            mock_db.return_value = [("mydb", True, None)]
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
            mock_up.return_value = [("order-svc", False, "unreachable")]

            await run_single_check(state, trigger_after_failures=2)

            assert state.get_status("upstream_health", "order-svc") == "alert"
            mock_dispatch.assert_called_once()
            kwargs = mock_dispatch.call_args.kwargs
            assert kwargs["resource_type"] == "upstream"
            assert kwargs["resource_id"] == "order-svc"
            assert kwargs["alert_type"] == "triggered"
            assert kwargs["target"] == "order-svc"
            assert kwargs["message"] == "Upstream 'order-svc' is down (no reachable node)."
            assert kwargs["display_target"] == "order-svc"

    @pytest.mark.asyncio
    async def test_upstream_with_no_weighted_nodes_says_so(self):
        """An empty node map is a config fault, not an unreachable backend."""
        state = AlertStateManager()
        state.update("upstream_health", "order-svc", is_healthy=False, trigger_after_failures=2)

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = []
            mock_up.return_value = [("order-svc", False, "no_nodes")]

            await run_single_check(state, trigger_after_failures=2)

            kwargs = mock_dispatch.call_args.kwargs
            assert kwargs["message"] == "Upstream 'order-svc' has no weighted nodes configured."
            # Same alert identity as any other upstream failure, so mutes and
            # recoveries keyed on it keep working.
            assert kwargs["rule_type"] == "upstream_health"
            assert kwargs["target"] == "order-svc"
            assert kwargs["monitor_label"] == "업스트림 헬스체크"

    @pytest.mark.asyncio
    async def test_upstream_recovery_message_is_unchanged(self):
        state = AlertStateManager()
        state.update("upstream_health", "order-svc", is_healthy=False, trigger_after_failures=1)

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_route_error_rate", new_callable=AsyncMock, return_value=[]), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = []
            mock_up.return_value = [("order-svc", True, None)]

            await run_single_check(state, trigger_after_failures=1)

            kwargs = mock_dispatch.call_args.kwargs
            assert kwargs["alert_type"] == "resolved"
            assert kwargs["message"] == "Upstream 'order-svc' recovered."

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
                mock_up.return_value = [("upstream-1", False, "unreachable")]

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
            mock_db.return_value = [("boot-db", False, None)]
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
            mock_db.side_effect = [[("boot-db", False, None)], [("boot-db", True, None)]]
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
    async def test_route_error_rate_maps_name_label_back_to_route_id(self):
        """With APISIX prefer_name the Prometheus route label carries the route
        *name*; alert state and per-resource recipients stay keyed by id."""
        from app.services import alert_checker
        state = AlertStateManager()
        state.update(
            "route_error_rate",
            "route-a",
            is_healthy=False,
            display_target="checkout (route-a)",
            trigger_after_failures=2,
        )
        alert_checker._ROUTE_LABEL_CACHE = {"route-a": "checkout"}
        alert_checker._ROUTE_ID_BY_NAME = {"checkout": "route-a"}
        alert_checker._ROUTE_LABEL_CACHE_TS = 9e18  # far future — skip refresh

        try:
            with patch("app.services.alert_checker._check_db_health", new=AsyncMock(return_value=[])), \
                 patch("app.services.alert_checker._check_upstream_health", new=AsyncMock(return_value=[])), \
                 patch("app.services.alert_checker._check_route_error_rate", new=AsyncMock(return_value=[("checkout", 12.5, 100.0)])), \
                 patch("app.services.alert_checker.async_session", return_value=_FakeSessionContext(_threshold_db(10.0))), \
                 patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
                await run_single_check(state, trigger_after_failures=2)
        finally:
            alert_checker._ROUTE_LABEL_CACHE = {}
            alert_checker._ROUTE_ID_BY_NAME = {}
            alert_checker._ROUTE_LABEL_CACHE_TS = 0.0

        mock_dispatch.assert_called_once()
        kwargs = mock_dispatch.call_args.kwargs
        assert kwargs["resource_id"] == "route-a"
        assert kwargs["target"] == "route-a"
        assert kwargs["display_target"] == "checkout (route-a)"

    @pytest.mark.asyncio
    async def test_route_error_rate_merges_old_and_new_labels_after_rename(self):
        """Right after a rename Prometheus reports the same route under both
        the old (id) and new (name) label for one window; the rows must merge
        into a single evaluation keyed by the route id — not double-count fail
        cycles or let the stale row resolve a genuinely failing alert."""
        from app.services import alert_checker
        state = AlertStateManager()
        state.update(
            "route_error_rate",
            "route-a",
            is_healthy=False,
            display_target="checkout (route-a)",
            trigger_after_failures=2,
        )
        alert_checker._ROUTE_LABEL_CACHE = {"route-a": "checkout"}
        alert_checker._ROUTE_ID_BY_NAME = {"checkout": "route-a"}
        alert_checker._ROUTE_LABEL_CACHE_TS = 9e18  # far future — skip refresh

        rows = [("route-a", 100.0, 10.0), ("checkout", 0.0, 90.0)]
        try:
            with patch("app.services.alert_checker._check_db_health", new=AsyncMock(return_value=[])), \
                 patch("app.services.alert_checker._check_upstream_health", new=AsyncMock(return_value=[])), \
                 patch("app.services.alert_checker._check_route_error_rate", new=AsyncMock(return_value=rows)), \
                 patch("app.services.alert_checker.async_session", return_value=_FakeSessionContext(_threshold_db(10.0))), \
                 patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
                await run_single_check(state, trigger_after_failures=2)
        finally:
            alert_checker._ROUTE_LABEL_CACHE = {}
            alert_checker._ROUTE_ID_BY_NAME = {}
            alert_checker._ROUTE_LABEL_CACHE_TS = 0.0

        # 10 errors over 100 requests = 10.0% — evaluated once, at/over the
        # 10.0% threshold, so the second unhealthy cycle dispatches.
        mock_dispatch.assert_called_once()
        kwargs = mock_dispatch.call_args.kwargs
        assert kwargs["resource_id"] == "route-a"
        assert kwargs["rate"] == 10.0

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
    async def test_prometheus_up_gauge_flips_on_failure_and_success(self):
        """#48(b): the checker's Prometheus liveness gauge tracks its query."""
        from prometheus_client import REGISTRY

        from app.services.alert_checker import _check_route_error_rate

        with patch(
            "app.services.prometheus_client.instant_query",
            new=AsyncMock(side_effect=RuntimeError("prom down")),
        ):
            assert await _check_route_error_rate() is None
        assert REGISTRY.get_sample_value("unibridge_alert_checker_prometheus_up") == 0.0

        # A successful query — even one that returns no traffic — flips it back.
        with patch(
            "app.services.prometheus_client.instant_query",
            new=AsyncMock(return_value=[]),
        ):
            assert await _check_route_error_rate() == []
        assert REGISTRY.get_sample_value("unibridge_alert_checker_prometheus_up") == 1.0

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
            alert_checker._ROUTE_ID_BY_NAME = {}
            alert_checker._ROUTE_LABEL_CACHE_TS = 0.0

    @pytest.mark.asyncio
    async def test_refresh_builds_reverse_name_map_skipping_collisions(self):
        from app.services import alert_checker
        alert_checker._ROUTE_LABEL_CACHE = {}
        alert_checker._ROUTE_ID_BY_NAME = {}
        alert_checker._ROUTE_LABEL_CACHE_TS = 0.0
        fake = {"items": [
            {"id": "r1", "name": "orders"},
            {"id": "r2", "name": "dup"},
            {"id": "r3", "name": "dup"},   # duplicate name → ambiguous, dropped
            {"id": "r4", "name": "r5"},    # name collides with a real route id
            {"id": "r5"},
            {"id": "same", "name": "same"},  # name == id → no mapping needed
        ]}
        with patch(
            "app.services.apisix_client.list_resources",
            new=AsyncMock(return_value=fake),
        ):
            await alert_checker._refresh_route_labels()
        try:
            assert alert_checker._ROUTE_ID_BY_NAME == {"orders": "r1"}
        finally:
            alert_checker._ROUTE_LABEL_CACHE = {}
            alert_checker._ROUTE_ID_BY_NAME = {}
            alert_checker._ROUTE_LABEL_CACHE_TS = 0.0

    @pytest.mark.asyncio
    async def test_resolve_route_id_translates_known_name(self):
        """prefer_name puts the route *name* in the Prometheus label; state and
        recipient lookups must stay keyed by route id."""
        from app.services import alert_checker
        alert_checker._ROUTE_LABEL_CACHE = {"r1": "orders"}
        alert_checker._ROUTE_ID_BY_NAME = {"orders": "r1"}
        alert_checker._ROUTE_LABEL_CACHE_TS = 9e18  # far future — skip refresh
        try:
            assert await alert_checker._resolve_route_id("orders") == "r1"
            assert await alert_checker._resolve_route_id("r1") == "r1"
            assert await alert_checker._resolve_route_id("mystery") == "mystery"
        finally:
            alert_checker._ROUTE_LABEL_CACHE = {}
            alert_checker._ROUTE_ID_BY_NAME = {}
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
        lambda: _async_return([("main-db", False, None)]),
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


class TestExternalServiceHealth:
    @pytest.mark.asyncio
    async def test_external_service_down_dispatches(self):
        from app.services.alert_checker import _check_service_health
        from app.services.server_monitor import ServiceSignal

        state = AlertStateManager()
        svc = SimpleNamespace(name="orders", enabled=True)
        signal = ServiceSignal(
            alert_type="external_service_down", target="orders", display="orders",
            is_healthy=False, severity="critical",
            message="External service 'orders' is unreachable (metrics scrape is down).",
            monitor_label="외부 서비스 상태",
            description="Order API",
        )
        with patch("app.services.alert_checker._load_service_monitoring",
                   new_callable=AsyncMock, return_value=([svc], 0)), \
             patch("app.services.alert_checker.server_monitor.evaluate_services",
                   new_callable=AsyncMock, return_value=[signal]), \
             patch("app.services.alert_checker._persist_state_safely", new_callable=AsyncMock), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            # trigger_after_failures=1 → first unhealthy observation fires immediately.
            await _check_service_health(state, trigger_after_failures=1)

        assert state.get_status("external_service_down", "orders") == "alert"
        mock_dispatch.assert_called_once()
        kwargs = mock_dispatch.call_args.kwargs
        assert kwargs["resource_type"] == "service"
        assert kwargs["resource_id"] == "orders"
        assert kwargs["alert_type"] == "triggered"
        assert kwargs["target"] == "orders"
        assert kwargs["severity"] == "critical"
        assert kwargs["target_description"] == "Order API"

    @pytest.mark.asyncio
    async def test_no_enabled_services_no_dispatch(self):
        from app.services.alert_checker import _check_service_health

        state = AlertStateManager()
        with patch("app.services.alert_checker._load_service_monitoring",
                   new_callable=AsyncMock, return_value=([], 0)), \
             patch("app.services.alert_checker.dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            await _check_service_health(state, trigger_after_failures=1)
        mock_dispatch.assert_not_called()


def _upstream_listing(*items):
    return patch(
        "app.services.apisix_client.list_resources",
        new=AsyncMock(return_value={"items": list(items)}),
    )


class TestUpstreamReachabilityProbe:
    """The upstream check must talk to the node, not just read APISIX config.

    Reading config only made "healthy" a tautology (any weighted node in the
    map = up), so a dead backend never alerted; and list-form ``nodes`` fell
    through the dict guard into a permanent false "down".
    """

    @pytest.mark.asyncio
    async def test_unreachable_node_is_unhealthy(self):
        import httpx
        from app.services import alert_checker

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with _upstream_listing({"id": "orders", "nodes": {"orders-api:8080": 1}}):
            result = await alert_checker._check_upstream_health(
                transport=httpx.MockTransport(handler)
            )

        assert result == [("orders", False, "unreachable")]

    @pytest.mark.asyncio
    async def test_reachable_node_is_healthy(self):
        import httpx
        from app.services import alert_checker

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"status": "ok"})

        with _upstream_listing({"id": "orders", "nodes": {"orders-api:8080": 1}}):
            result = await alert_checker._check_upstream_health(
                transport=httpx.MockTransport(handler)
            )

        assert result == [("orders", True, None)]
        assert seen == ["http://orders-api:8080/health"]

    @pytest.mark.asyncio
    async def test_any_http_status_counts_as_reachable(self):
        """A 404 on /health still proves the port is open and speaking HTTP."""
        import httpx
        from app.services import alert_checker

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="not found")

        with _upstream_listing({"id": "orders", "nodes": {"orders-api:8080": 1}}):
            result = await alert_checker._check_upstream_health(
                transport=httpx.MockTransport(handler)
            )

        assert result == [("orders", True, None)]

    @pytest.mark.asyncio
    async def test_timeout_is_unhealthy(self):
        import httpx
        from app.services import alert_checker

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        with _upstream_listing({"id": "orders", "nodes": {"orders-api:8080": 1}}):
            result = await alert_checker._check_upstream_health(
                transport=httpx.MockTransport(handler)
            )

        assert result == [("orders", False, "unreachable")]

    @pytest.mark.asyncio
    async def test_list_form_nodes_are_probed_instead_of_reported_down(self):
        """Regression: list-form ``nodes`` used to be a permanent false "down"."""
        import httpx
        from app.services import alert_checker

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200)

        listing = {
            "id": "orders",
            "nodes": [{"host": "orders-api", "port": 8080, "weight": 1}],
        }
        with _upstream_listing(listing):
            result = await alert_checker._check_upstream_health(
                transport=httpx.MockTransport(handler)
            )

        assert result == [("orders", True, None)]
        assert seen == ["http://orders-api:8080/health"]

    @pytest.mark.asyncio
    async def test_list_form_nodes_still_report_down_when_unreachable(self):
        import httpx
        from app.services import alert_checker

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        listing = {
            "id": "orders",
            "nodes": [{"host": "orders-api", "port": 8080, "weight": 1}],
        }
        with _upstream_listing(listing):
            result = await alert_checker._check_upstream_health(
                transport=httpx.MockTransport(handler)
            )

        assert result == [("orders", False, "unreachable")]

    @pytest.mark.asyncio
    async def test_one_reachable_node_is_enough(self):
        import httpx
        from app.services import alert_checker

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "dead":
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200)

        listing = {"id": "orders", "nodes": {"dead:8080": 1, "alive:8080": 1}}
        with _upstream_listing(listing):
            result = await alert_checker._check_upstream_health(
                transport=httpx.MockTransport(handler)
            )

        assert result == [("orders", True, None)]

    @pytest.mark.asyncio
    async def test_upstream_without_weighted_nodes_is_unhealthy(self):
        import httpx
        from app.services import alert_checker

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no node should be probed")

        with _upstream_listing({"id": "orders", "nodes": {}}):
            result = await alert_checker._check_upstream_health(
                transport=httpx.MockTransport(handler)
            )

        assert result == [("orders", False, "no_nodes")]

    @pytest.mark.asyncio
    async def test_non_http_upstream_with_nodes_is_healthy_without_probing(self):
        """A GET against a grpc/tcp/kafka port proves nothing, so it is not sent.

        Deliberate scope limit: those upstreams fall back to the config-only
        judgment, which is why their only detectable failure is "no nodes".
        """
        import httpx
        from app.services import alert_checker

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a non-HTTP upstream must not be probed")

        for scheme in ("grpc", "grpcs", "tcp", "tls", "udp", "kafka"):
            listing = {"id": "orders", "scheme": scheme, "nodes": {"orders-grpc:9090": 1}}
            with _upstream_listing(listing):
                result = await alert_checker._check_upstream_health(
                    transport=httpx.MockTransport(handler)
                )
            assert result == [("orders", True, None)], scheme

    @pytest.mark.asyncio
    async def test_non_http_upstream_without_weighted_nodes_is_unhealthy(self):
        import httpx
        from app.services import alert_checker

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("a non-HTTP upstream must not be probed")

        listing = {
            "id": "orders",
            "scheme": "grpc",
            "nodes": [{"host": "orders-grpc", "port": 9090, "weight": 0}],
        }
        with _upstream_listing(listing):
            result = await alert_checker._check_upstream_health(
                transport=httpx.MockTransport(handler)
            )

        assert result == [("orders", False, "no_nodes")]

    @pytest.mark.asyncio
    async def test_litellm_upstream_uses_the_liveliness_path(self):
        import httpx
        from app.services import alert_checker

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200)

        with _upstream_listing({"id": "litellm", "nodes": {"litellm:4000": 1}}):
            await alert_checker._check_upstream_health(
                transport=httpx.MockTransport(handler)
            )

        assert seen == ["/health/liveliness"]

    @pytest.mark.asyncio
    async def test_host_header_follows_pass_host_node(self):
        import httpx
        from app.services import alert_checker

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["Host"])
            return httpx.Response(200)

        listing = {
            "id": "orders",
            "pass_host": "node",
            "scheme": "https",
            "nodes": {"orders-api:8443": 1},
        }
        with _upstream_listing(listing):
            await alert_checker._check_upstream_health(
                transport=httpx.MockTransport(handler)
            )

        assert seen == ["orders-api"]

    @pytest.mark.asyncio
    async def test_one_broken_upstream_does_not_abort_the_others(self):
        """A probe raising outside the transport must not lose the whole list."""
        import httpx
        from app.services import alert_checker

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        real_probe = alert_checker._probe_upstream

        async def flaky_probe(client, limiter, upstream):
            if upstream.get("id") == "broken":
                raise RuntimeError("probe bug")
            return await real_probe(client, limiter, upstream)

        listing = (
            {"id": "broken", "nodes": {"a:80": 1}},
            {"id": "orders", "nodes": {"orders-api:8080": 1}},
        )
        with _upstream_listing(*listing), \
             patch("app.services.alert_checker._probe_upstream", new=flaky_probe):
            result = await alert_checker._check_upstream_health(
                transport=httpx.MockTransport(handler)
            )

        assert result == [("orders", True, None)]


class TestActiveInstanceGating:
    """Blue/green: only the active color may run side-effectful cycles."""

    @staticmethod
    def _loop_patches(active, *, monotonic, interval=60):
        stop = RuntimeError("stop loop")
        delays: list[float] = []

        async def stop_after_sleep(delay: float):
            delays.append(delay)
            if len(delays) >= len(monotonic) // 2:
                raise stop

        return delays, (
            patch("app.services.alert_checker.is_active_instance",
                  new=AsyncMock(side_effect=active)),
            patch("app.services.alert_checker._get_check_interval_seconds",
                  new=AsyncMock(return_value=interval)),
            patch("app.services.alert_checker._get_trigger_after_failures",
                  new=AsyncMock(return_value=2)),
            patch("app.services.alert_checker._monotonic", side_effect=monotonic),
            patch("app.services.alert_checker.maybe_send_gpu_util_report",
                  new=AsyncMock(return_value=None)),
            patch("app.services.alert_checker.asyncio.sleep",
                  new=AsyncMock(side_effect=stop_after_sleep)),
        )

    @pytest.mark.asyncio
    async def test_standby_color_skips_the_check_but_keeps_its_cadence(self):
        from app.services import alert_checker

        state = AlertStateManager()
        delays, patches = self._loop_patches([False], monotonic=[100.0, 115.0])
        with patch("app.services.alert_checker.run_single_check",
                   new_callable=AsyncMock) as check:
            for p in patches:
                p.start()
            try:
                task = await alert_checker.start_checker(state)
                with pytest.raises(RuntimeError, match="stop loop"):
                    await task
            finally:
                for p in patches:
                    p.stop()

        check.assert_not_called()
        # Still wakes on the normal interval, so a promotion is picked up fast.
        assert delays == [45.0]

    @pytest.mark.asyncio
    async def test_active_color_runs_the_check(self):
        from app.services import alert_checker

        state = AlertStateManager()
        delays, patches = self._loop_patches([True], monotonic=[100.0, 115.0])
        with patch("app.services.alert_checker.run_single_check",
                   new_callable=AsyncMock) as check:
            for p in patches:
                p.start()
            try:
                task = await alert_checker.start_checker(state)
                with pytest.raises(RuntimeError, match="stop loop"):
                    await task
            finally:
                for p in patches:
                    p.stop()

        check.assert_awaited_once()
        assert delays == [45.0]

    @pytest.mark.asyncio
    async def test_promotion_is_picked_up_without_a_restart(self):
        """Standby → active between cycles must start checking on the next tick."""
        from app.services import alert_checker

        state = AlertStateManager()
        delays, patches = self._loop_patches(
            [False, True], monotonic=[100.0, 115.0, 160.0, 175.0]
        )
        with patch("app.services.alert_checker.run_single_check",
                   new_callable=AsyncMock) as check:
            for p in patches:
                p.start()
            try:
                task = await alert_checker.start_checker(state)
                with pytest.raises(RuntimeError, match="stop loop"):
                    await task
            finally:
                for p in patches:
                    p.stop()

        assert check.await_count == 1
        assert delays == [45.0, 45.0]

    @pytest.mark.asyncio
    async def test_daily_gpu_report_runs_only_on_the_active_color(self):
        from app.services import alert_checker

        for active, expected in ((True, 1), (False, 0)):
            state = AlertStateManager()
            _delays, patches = self._loop_patches([active], monotonic=[100.0, 115.0])
            for p in patches:
                p.start()
            try:
                # Entered after the shared patches so this mock is the live one.
                with patch("app.services.alert_checker.run_single_check", new_callable=AsyncMock), \
                     patch("app.services.alert_checker.maybe_send_gpu_util_report",
                           new_callable=AsyncMock) as report:
                    task = await alert_checker.start_checker(state)
                    with pytest.raises(RuntimeError, match="stop loop"):
                        await task
            finally:
                for p in patches:
                    p.stop()
            assert report.await_count == expected

    @pytest.mark.asyncio
    async def test_report_and_health_checks_cannot_break_each_other(self):
        """Each runs in its own try, so one failing never skips the other."""
        from app.services import alert_checker

        state = AlertStateManager()
        delays, patches = self._loop_patches([True], monotonic=[100.0, 115.0])
        for p in patches:
            p.start()
        try:
            with patch("app.services.alert_checker.run_single_check",
                       new=AsyncMock(side_effect=RuntimeError("check exploded"))) as check, \
                 patch("app.services.alert_checker.maybe_send_gpu_util_report",
                       new=AsyncMock(side_effect=RuntimeError("report exploded"))) as report:
                task = await alert_checker.start_checker(state)
                with pytest.raises(RuntimeError, match="stop loop"):
                    await task
        finally:
            for p in patches:
                p.stop()

        check.assert_awaited_once()
        report.assert_awaited_once()
        # The loop kept its cadence rather than dying on either failure.
        assert delays == [45.0]


class TestFailedDispatchRearm:
    """#15: a trigger whose delivery fails must be retried, not lost.

    ``dispatch_alert`` now reports delivery (True = sent, None = skipped,
    False = failed). The checker re-arms ``pending_notify`` only for a failed
    *trigger*, so the next cycle re-announces it; a skip or a success does not
    re-arm, and neither does a failed recovery.
    """

    @staticmethod
    def _db_down_patches(dispatch_result):
        return (
            patch("app.services.alert_checker._check_db_health",
                  new=AsyncMock(return_value=[("mydb", False, None)])),
            patch("app.services.alert_checker._check_upstream_health",
                  new=AsyncMock(return_value=[])),
            patch("app.services.alert_checker._check_route_error_rate",
                  new=AsyncMock(return_value=[])),
            patch("app.services.alert_checker.dispatch_alert",
                  new=AsyncMock(return_value=dispatch_result)),
        )

    @pytest.mark.asyncio
    async def test_failed_trigger_is_reannounced_next_cycle(self):
        state = AlertStateManager()
        # Seed fail_count=1 so the first cycle crosses N=2 and triggers.
        state.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        db_p, up_p, route_p, disp_p = self._db_down_patches(False)
        with db_p, up_p, route_p, disp_p as mock_dispatch:
            await run_single_check(state, trigger_after_failures=2)
            assert mock_dispatch.await_count == 1
            assert mock_dispatch.await_args.kwargs["alert_type"] == "triggered"
            # Failed delivery re-armed the incident.
            assert state.get_pending_notify("db_health", "mydb") is True

            # Next cycle has no transition of its own, but the pending flag
            # drives a fresh announcement.
            await run_single_check(state, trigger_after_failures=2)
            assert mock_dispatch.await_count == 2
            assert mock_dispatch.await_args.kwargs["alert_type"] == "triggered"
            assert state.get_pending_notify("db_health", "mydb") is True

    @pytest.mark.asyncio
    async def test_successful_trigger_is_not_reannounced(self):
        state = AlertStateManager()
        state.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        db_p, up_p, route_p, disp_p = self._db_down_patches(True)
        with db_p, up_p, route_p, disp_p as mock_dispatch:
            await run_single_check(state, trigger_after_failures=2)
            assert mock_dispatch.await_count == 1
            assert state.get_pending_notify("db_health", "mydb") is False
            await run_single_check(state, trigger_after_failures=2)
            # No re-announcement: the first delivery succeeded.
            assert mock_dispatch.await_count == 1

    @pytest.mark.asyncio
    async def test_skipped_dispatch_is_not_treated_as_failure(self):
        state = AlertStateManager()
        state.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        db_p, up_p, route_p, disp_p = self._db_down_patches(None)
        with db_p, up_p, route_p, disp_p as mock_dispatch:
            await run_single_check(state, trigger_after_failures=2)
            assert mock_dispatch.await_count == 1
            # None (skipped — e.g. no recipients) is not a delivery failure.
            assert state.get_pending_notify("db_health", "mydb") is False
            await run_single_check(state, trigger_after_failures=2)
            assert mock_dispatch.await_count == 1

    @pytest.mark.asyncio
    async def test_failed_resolved_is_not_rearmed(self):
        state = AlertStateManager()
        # An announced, active alert (pending_notify False = already announced).
        state.update("db_health", "mydb", is_healthy=False, trigger_after_failures=1)
        assert state.get_status("db_health", "mydb") == "alert"

        with patch("app.services.alert_checker._check_db_health",
                   new=AsyncMock(return_value=[("mydb", True, None)])), \
             patch("app.services.alert_checker._check_upstream_health",
                   new=AsyncMock(return_value=[])), \
             patch("app.services.alert_checker._check_route_error_rate",
                   new=AsyncMock(return_value=[])), \
             patch("app.services.alert_checker.dispatch_alert",
                   new=AsyncMock(return_value=False)) as mock_dispatch:
            await run_single_check(state, trigger_after_failures=1)

        mock_dispatch.assert_awaited_once()
        assert mock_dispatch.await_args.kwargs["alert_type"] == "resolved"
        # A lost recovery is self-correcting: the target is healthy, so we do
        # not re-arm (that would risk a spurious re-trigger).
        assert state.get_status("db_health", "mydb") == "ok"
        assert state.get_pending_notify("db_health", "mydb") is False
