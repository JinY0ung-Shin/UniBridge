from __future__ import annotations

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
