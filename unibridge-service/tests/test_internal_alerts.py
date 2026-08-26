"""Tests for the internal Alertmanager webhook receiver.

The router is exercised on a bare FastAPI app rather than the real one: it must
work independently of main.py's registration (and of the app fixture's DB
override), since the only thing it touches is the dispatcher, which is patched.
"""
from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.routers import internal_alerts

TOKEN = "secret-webhook-token"

FIRING_PAYLOAD = {
    "receiver": "unibridge-webhook",
    "status": "firing",
    "commonLabels": {"alertname": "UniBridgeServiceDown", "severity": "critical"},
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "UniBridgeServiceDown",
                "severity": "critical",
                "job": "unibridge-service",
                "instance": "unibridge-service:8000",
            },
            "annotations": {
                "summary": "UniBridge service is down",
                "description": "Prometheus cannot scrape unibridge-service metrics.",
            },
            "startsAt": "2026-08-25T10:00:00.000Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "fingerprint": "abc123",
        }
    ],
}


@pytest.fixture
def webhook_app() -> FastAPI:
    app = FastAPI()
    app.include_router(internal_alerts.router)
    return app


@pytest.fixture
async def webhook_client(webhook_app):
    transport = ASGITransport(app=webhook_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(settings, "ALERTMANAGER_WEBHOOK_TOKEN", TOKEN)
    return TOKEN


@pytest.fixture
def dispatched(monkeypatch):
    """Capture dispatch_alert kwargs instead of sending mail."""
    calls: list[dict] = []

    async def fake_dispatch_alert(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        "app.services.alert_owner_dispatcher.dispatch_alert", fake_dispatch_alert
    )
    return calls


@pytest.fixture
def fresh_disabled_warning(monkeypatch):
    """Restore the module-level once-per-process flag around a test."""
    monkeypatch.setattr(internal_alerts, "_disabled_warning_logged", False)


def auth(value: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def disabled_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "ALERTMANAGER_WEBHOOK_TOKEN" in r.getMessage()
    ]


class TestAuth:
    async def test_disabled_when_token_unset(self, webhook_client, monkeypatch):
        monkeypatch.setattr(settings, "ALERTMANAGER_WEBHOOK_TOKEN", "")

        resp = await webhook_client.post(
            "/internal/alertmanager", json=FIRING_PAYLOAD, headers=auth()
        )

        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"]

    async def test_missing_authorization_header(self, webhook_client, token):
        resp = await webhook_client.post("/internal/alertmanager", json=FIRING_PAYLOAD)

        assert resp.status_code == 401
        assert resp.headers["WWW-Authenticate"] == "Bearer"

    async def test_wrong_token(self, webhook_client, token):
        resp = await webhook_client.post(
            "/internal/alertmanager", json=FIRING_PAYLOAD, headers=auth("nope")
        )

        assert resp.status_code == 401

    async def test_wrong_scheme(self, webhook_client, token):
        resp = await webhook_client.post(
            "/internal/alertmanager",
            json=FIRING_PAYLOAD,
            headers={"Authorization": f"Basic {TOKEN}"},
        )

        assert resp.status_code == 401

    async def test_disabled_check_precedes_auth(self, webhook_client, monkeypatch):
        """An unconfigured deployment says 503 even for an unauthenticated call."""
        monkeypatch.setattr(settings, "ALERTMANAGER_WEBHOOK_TOKEN", "")

        resp = await webhook_client.post("/internal/alertmanager", json=FIRING_PAYLOAD)

        assert resp.status_code == 503

    async def test_disabled_logs_once_per_process(
        self, webhook_client, monkeypatch, caplog, fresh_disabled_warning
    ):
        """The 503 is a silent no-op otherwise — say why, but only once.

        Alertmanager retries a failed group every few minutes indefinitely, so a
        per-request warning would bury the logs it exists to draw attention to.
        """
        monkeypatch.setattr(settings, "ALERTMANAGER_WEBHOOK_TOKEN", "")

        with caplog.at_level(logging.WARNING, logger=internal_alerts.__name__):
            first = await webhook_client.post(
                "/internal/alertmanager", json=FIRING_PAYLOAD, headers=auth()
            )
            second = await webhook_client.post(
                "/internal/alertmanager", json=FIRING_PAYLOAD, headers=auth()
            )

        assert (first.status_code, second.status_code) == (503, 503)
        warnings = disabled_warnings(caplog)
        assert len(warnings) == 1
        assert "disabled" in warnings[0]

    async def test_configured_token_logs_no_disabled_warning(
        self, webhook_client, token, dispatched, caplog, fresh_disabled_warning
    ):
        with caplog.at_level(logging.WARNING, logger=internal_alerts.__name__):
            resp = await webhook_client.post(
                "/internal/alertmanager", json=FIRING_PAYLOAD, headers=auth()
            )

        assert resp.status_code == 200
        assert disabled_warnings(caplog) == []


class TestDispatch:
    async def test_firing_alert_dispatches_to_admins(
        self, webhook_client, token, dispatched
    ):
        resp = await webhook_client.post(
            "/internal/alertmanager", json=FIRING_PAYLOAD, headers=auth()
        )

        assert resp.status_code == 200
        assert resp.json() == {"received": 1, "dispatched": 1}
        assert len(dispatched) == 1
        kwargs = dispatched[0]
        # Transition string the mail template + history filter + UI badge expect.
        assert kwargs["alert_type"] == "triggered"
        # Not in alert_owner_dispatcher.ASSIGNEE_RESOURCE_TYPES → admins only.
        assert kwargs["resource_type"] == "infra"
        assert kwargs["resource_id"] == "UniBridgeServiceDown"
        assert kwargs["rule_type"] == "prometheus_alert"
        assert kwargs["target"] == "UniBridgeServiceDown"
        assert kwargs["severity"] == "critical"
        assert kwargs["display_target"] == "UniBridgeServiceDown (unibridge-service:8000)"
        assert kwargs["monitor_label"] == "Prometheus 알림"
        assert "UniBridge service is down" in kwargs["message"]
        assert "cannot scrape unibridge-service metrics" in kwargs["message"]
        assert "firing" in kwargs["message"]

    async def test_resolved_alert_dispatches_resolved(
        self, webhook_client, token, dispatched
    ):
        payload = {
            "status": "resolved",
            "alerts": [
                {
                    "status": "resolved",
                    "labels": {"alertname": "KeycloakDbDown", "severity": "critical"},
                    "annotations": {"summary": "Keycloak database TCP probe failed"},
                    "endsAt": "2026-08-25T10:05:00.000Z",
                }
            ],
        }

        resp = await webhook_client.post(
            "/internal/alertmanager", json=payload, headers=auth()
        )

        assert resp.status_code == 200
        assert resp.json() == {"received": 1, "dispatched": 1}
        assert dispatched[0]["alert_type"] == "resolved"
        assert "resolved" in dispatched[0]["message"]

    async def test_unknown_status_is_treated_as_firing(
        self, webhook_client, token, dispatched
    ):
        payload = {"alerts": [{"labels": {"alertname": "MysteryAlert"}}]}

        resp = await webhook_client.post(
            "/internal/alertmanager", json=payload, headers=auth()
        )

        assert resp.status_code == 200
        assert dispatched[0]["alert_type"] == "triggered"
        assert dispatched[0]["target"] == "MysteryAlert"

    async def test_multiple_alerts_all_dispatched(
        self, webhook_client, token, dispatched
    ):
        payload = {
            "alerts": [
                {"status": "firing", "labels": {"alertname": "A"}},
                {"status": "firing", "labels": {"alertname": "B"}},
            ]
        }

        resp = await webhook_client.post(
            "/internal/alertmanager", json=payload, headers=auth()
        )

        assert resp.json() == {"received": 2, "dispatched": 2}
        assert [c["target"] for c in dispatched] == ["A", "B"]

    async def test_empty_alerts_list_is_a_noop(self, webhook_client, token, dispatched):
        resp = await webhook_client.post(
            "/internal/alertmanager", json={"alerts": []}, headers=auth()
        )

        assert resp.status_code == 200
        assert resp.json() == {"received": 0, "dispatched": 0}
        assert dispatched == []

    async def test_annotation_quotes_and_newlines_are_neutralised(
        self, webhook_client, token, dispatched
    ):
        """The mail payload template is rendered by string substitution."""
        payload = {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "Weird"},
                    "annotations": {
                        "summary": 'quote " and\nnewline',
                        "description": "back\\slash",
                    },
                }
            ]
        }

        await webhook_client.post(
            "/internal/alertmanager", json=payload, headers=auth()
        )

        message = dispatched[0]["message"]
        assert '"' not in message
        assert "\n" not in message
        assert "\\" not in message
        assert "quote ' and newline" in message


class TestRobustness:
    async def test_malformed_json_body(self, webhook_client, token, dispatched):
        resp = await webhook_client.post(
            "/internal/alertmanager",
            content=b"{not json",
            headers={**auth(), "Content-Type": "application/json"},
        )

        assert resp.status_code == 400
        assert "valid JSON" in resp.json()["detail"]
        assert dispatched == []

    async def test_non_object_body(self, webhook_client, token, dispatched):
        resp = await webhook_client.post(
            "/internal/alertmanager", json=["not", "an", "object"], headers=auth()
        )

        assert resp.status_code == 400
        assert dispatched == []

    async def test_missing_alerts_array(self, webhook_client, token, dispatched):
        resp = await webhook_client.post(
            "/internal/alertmanager", json={"status": "firing"}, headers=auth()
        )

        assert resp.status_code == 400
        assert "alerts" in resp.json()["detail"]
        assert dispatched == []

    async def test_non_object_alert_entry_is_skipped(
        self, webhook_client, token, dispatched
    ):
        payload = {"alerts": ["oops", {"status": "firing", "labels": {"alertname": "B"}}]}

        resp = await webhook_client.post(
            "/internal/alertmanager", json=payload, headers=auth()
        )

        assert resp.json() == {"received": 2, "dispatched": 1}
        assert [c["target"] for c in dispatched] == ["B"]

    async def test_one_failing_dispatch_does_not_abort_the_batch(
        self, webhook_client, token, monkeypatch
    ):
        calls: list[str] = []

        async def flaky_dispatch_alert(**kwargs):
            calls.append(kwargs["target"])
            if kwargs["target"] == "A":
                raise RuntimeError("mail channel exploded")

        monkeypatch.setattr(
            "app.services.alert_owner_dispatcher.dispatch_alert", flaky_dispatch_alert
        )
        payload = {
            "alerts": [
                {"status": "firing", "labels": {"alertname": "A"}},
                {"status": "firing", "labels": {"alertname": "B"}},
                {"status": "resolved", "labels": {"alertname": "C"}},
            ]
        }

        resp = await webhook_client.post(
            "/internal/alertmanager", json=payload, headers=auth()
        )

        assert resp.status_code == 200
        # Every alert was attempted; only the failing one is missing from the count.
        assert calls == ["A", "B", "C"]
        assert resp.json() == {"received": 3, "dispatched": 2}

    async def test_batch_over_cap_is_truncated(self, webhook_client, token, dispatched):
        over = internal_alerts.MAX_ALERTS_PER_REQUEST + 5
        payload = {
            "alerts": [
                {"status": "firing", "labels": {"alertname": f"A{i}"}}
                for i in range(over)
            ]
        }

        resp = await webhook_client.post(
            "/internal/alertmanager", json=payload, headers=auth()
        )

        assert resp.json() == {
            "received": over,
            "dispatched": internal_alerts.MAX_ALERTS_PER_REQUEST,
        }
        assert len(dispatched) == internal_alerts.MAX_ALERTS_PER_REQUEST

    async def test_missing_labels_still_dispatches(
        self, webhook_client, token, dispatched
    ):
        resp = await webhook_client.post(
            "/internal/alertmanager",
            json={"alerts": [{"status": "firing"}]},
            headers=auth(),
        )

        assert resp.json() == {"received": 1, "dispatched": 1}
        assert dispatched[0]["target"] == "UnnamedAlert"
        assert dispatched[0]["severity"] is None


class TestPipelineContract:
    """Guard the values that couple this router to the existing mail pipeline."""

    def test_alert_type_strings_match_the_checker(self):
        from app.services import alert_checker  # noqa: F401  (import sanity)

        assert internal_alerts.ALERT_TYPE_TRIGGERED == "triggered"
        assert internal_alerts.ALERT_TYPE_RESOLVED == "resolved"

    def test_resource_type_routes_to_admins_only(self):
        from app.services.alert_owner_dispatcher import ASSIGNEE_RESOURCE_TYPES

        # Assignee lookup is skipped for unknown resource types, leaving the
        # global admin list as the only recipients.
        assert internal_alerts.RESOURCE_TYPE not in ASSIGNEE_RESOURCE_TYPES

    def test_history_column_limits_are_respected(self):
        from app.models import AlertHistory

        columns = AlertHistory.__table__.columns
        assert len(internal_alerts.RESOURCE_TYPE) <= columns["resource_type"].type.length
        assert len(internal_alerts.RULE_TYPE) <= columns["rule_type"].type.length
        assert internal_alerts._MAX_TARGET <= columns["target"].type.length
        assert internal_alerts._MAX_DISPLAY_TARGET <= columns["display_target"].type.length
        assert internal_alerts._MAX_SEVERITY <= columns["severity"].type.length
