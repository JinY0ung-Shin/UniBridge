"""Tests for alert_checker module."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from app.services.alert_checker import run_single_check
from app.services.alert_state import AlertStateManager


class TestAlertChecker:
    @pytest.mark.asyncio
    async def test_db_health_triggered(self):
        state = AlertStateManager()

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_error_rate", new_callable=AsyncMock) as mock_err, \
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

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_error_rate", new_callable=AsyncMock) as mock_err, \
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
             patch("app.services.alert_checker._dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = [("mydb", True)]
            mock_up.return_value = []
            mock_err.return_value = []

            await run_single_check(state)

            mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_upstream_health_triggered(self):
        state = AlertStateManager()

        with patch("app.services.alert_checker._check_db_health", new_callable=AsyncMock) as mock_db, \
             patch("app.services.alert_checker._check_upstream_health", new_callable=AsyncMock) as mock_up, \
             patch("app.services.alert_checker._check_error_rate", new_callable=AsyncMock) as mock_err, \
             patch("app.services.alert_checker._dispatch_alert", new_callable=AsyncMock) as mock_dispatch:
            mock_db.return_value = []
            mock_up.return_value = [("order-svc", False)]
            mock_err.return_value = []

            await run_single_check(state)

            assert state.get_status("upstream_health", "order-svc") == "alert"
            mock_dispatch.assert_called_once()
            assert mock_dispatch.call_args[1]["rule_type"] == "upstream_health"
