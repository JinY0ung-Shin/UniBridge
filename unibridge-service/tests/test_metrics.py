from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from app import metrics


def _output(registry: CollectorRegistry) -> str:
    return generate_latest(registry).decode("utf-8")


def test_query_metrics_record_duration_and_returned_rows() -> None:
    registry = CollectorRegistry()
    recorder = metrics.create_metrics(registry=registry)

    recorder.record_query(
        db_alias="analytics",
        db_type="postgres",
        status="success",
        duration_seconds=0.25,
        row_count=7,
    )

    output = _output(registry)
    assert (
        'unibridge_query_duration_seconds_count{db_alias="analytics",'
        'db_type="postgres",status="success"} 1.0'
    ) in output
    assert (
        'unibridge_query_rows_returned_count{db_alias="analytics",db_type="postgres"} 1.0'
    ) in output
    assert (
        'unibridge_query_rows_returned_sum{db_alias="analytics",db_type="postgres"} 7.0'
    ) in output


def test_alert_dispatch_metric_records_rule_channel_and_status() -> None:
    registry = CollectorRegistry()
    recorder = metrics.create_metrics(registry=registry)

    recorder.record_alert_dispatch(
        rule_id=42,
        channel_type="webhook",
        status="failure",
    )

    assert (
        'unibridge_alert_dispatch_total{channel_type="webhook",rule_id="42",status="failure"} 1.0'
    ) in _output(registry)


def test_pool_and_meta_db_gauges_record_current_values() -> None:
    registry = CollectorRegistry()
    recorder = metrics.create_metrics(registry=registry)

    recorder.set_connection_pool_in_use(db_alias="analytics", in_use=3)
    recorder.set_meta_db_up(False)

    output = _output(registry)
    assert 'unibridge_connection_pool_in_use{db_alias="analytics"} 3.0' in output
    assert "unibridge_meta_db_up 0.0" in output


def test_metrics_clamp_negative_observations_and_record_audit_write() -> None:
    registry = CollectorRegistry()
    recorder = metrics.create_metrics(registry=registry)

    recorder.record_query(
        db_alias="analytics",
        db_type="postgres",
        status="failure",
        duration_seconds=-1,
        row_count=-5,
    )
    recorder.set_connection_pool_in_use(db_alias="analytics", in_use=-2)
    recorder.record_audit_log_write(status="failure")

    output = _output(registry)
    assert (
        'unibridge_query_duration_seconds_sum{db_alias="analytics",'
        'db_type="postgres",status="failure"} 0.0'
    ) in output
    assert (
        'unibridge_query_rows_returned_sum{db_alias="analytics",db_type="postgres"} 0.0'
    ) in output
    assert 'unibridge_connection_pool_in_use{db_alias="analytics"} 0.0' in output
    assert 'unibridge_audit_log_write_total{status="failure"} 1.0' in output


def test_module_level_metric_helpers_delegate_to_recorder(monkeypatch) -> None:
    recorder = MagicMock()
    monkeypatch.setattr(metrics, "recorder", recorder)

    metrics.record_query(
        db_alias="warehouse",
        db_type="clickhouse",
        status="success",
        duration_seconds=0.5,
        row_count=3,
    )
    metrics.record_alert_dispatch(
        rule_id="rule-1", channel_type="slack", status="success"
    )
    metrics.record_audit_log_write(status="success")
    metrics.set_connection_pool_in_use(db_alias="warehouse", in_use=2)
    metrics.set_meta_db_up(True)

    recorder.record_query.assert_called_once_with(
        db_alias="warehouse",
        db_type="clickhouse",
        status="success",
        duration_seconds=0.5,
        row_count=3,
    )
    recorder.record_alert_dispatch.assert_called_once_with(
        rule_id="rule-1", channel_type="slack", status="success"
    )
    recorder.record_audit_log_write.assert_called_once_with(status="success")
    recorder.set_connection_pool_in_use.assert_called_once_with(
        db_alias="warehouse", in_use=2
    )
    recorder.set_meta_db_up.assert_called_once_with(True)


class _HealthSession:
    def __init__(self, execute: AsyncMock) -> None:
        self.execute = execute

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.parametrize("probe_fails", [False, True])
async def test_monitor_meta_db_health_records_probe_result(
    monkeypatch, caplog, probe_fails
) -> None:
    from app import database

    execute = AsyncMock()
    if probe_fails:
        execute.side_effect = RuntimeError("database offline")
    monkeypatch.setattr(database, "async_session", lambda: _HealthSession(execute))
    set_health = MagicMock()
    monkeypatch.setattr(metrics, "set_meta_db_up", set_health)

    async def _stop_after_first_probe(_interval):
        raise asyncio.CancelledError

    monkeypatch.setattr(metrics.asyncio, "sleep", _stop_after_first_probe)

    with pytest.raises(asyncio.CancelledError):
        await metrics.monitor_meta_db_health(interval_seconds=3)

    execute.assert_awaited_once()
    set_health.assert_called_once_with(not probe_fails)
    if probe_fails:
        assert "Metadata database health probe failed" in caplog.text
