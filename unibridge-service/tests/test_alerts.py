"""Tests for the health-check alert system."""
from __future__ import annotations

import socket

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import ALL_PERMISSIONS
from app.models import AlertChannel, AlertHistory, AlertSettings, ResourceOwner
from app.schemas import (
    AlertChannelCreate,
    AlertSettingsUpdate,
    AlertStatusResponse,
    RecipientTestRequest,
    ResourceOwnerUpsert,
)
from app.services.alert_state import AlertStateManager


class TestAlertModels:
    def test_alert_channel_columns(self):
        ch = AlertChannel(name="test", webhook_url="http://example.com/hook", payload_template='{}')
        assert ch.name == "test"
        assert ch.webhook_url == "http://example.com/hook"
        assert ch.enabled is True

    def test_alert_settings_columns(self):
        settings = AlertSettings(
            id=1,
            admin_emails='["admin@example.com"]',
            route_error_threshold_pct=10.0,
            check_interval_seconds=60,
        )
        assert settings.admin_emails == '["admin@example.com"]'
        assert settings.route_error_threshold_pct == 10.0
        # No fallback owner group on the simplified settings model.
        assert not hasattr(AlertSettings, "fallback_owner_group_id")

    def test_resource_owner_columns(self):
        owner = ResourceOwner(
            resource_type="db", resource_id="mydb", emails='["owner@example.com"]'
        )
        assert owner.resource_type == "db"
        assert owner.resource_id == "mydb"
        assert owner.emails == '["owner@example.com"]'

    def test_alert_history_columns(self):
        h = AlertHistory(channel_id=1, alert_type="triggered", target="mydb", message="down")
        assert h.alert_type == "triggered"
        assert h.success is None
        # History no longer carries a rule_id column.
        assert not hasattr(AlertHistory, "rule_id")


class TestAlertSchemas:
    def test_channel_create_valid(self):
        ch = AlertChannelCreate(
            name="email",
            webhook_url="http://mail.internal/api/send",
            payload_template='{"to":"{{recipients}}","subject":"{{alert_type}}"}',
        )
        assert ch.name == "email"
        assert ch.headers is None
        assert ch.enabled is True

    def test_settings_update_dedupes_admin_emails(self):
        body = AlertSettingsUpdate(
            admin_emails=[" a@x.com ", "a@x.com", "b@x.com", "", " b@x.com "],
        )
        assert body.admin_emails == ["a@x.com", "b@x.com"]

    def test_settings_update_allows_empty_admin_emails(self):
        body = AlertSettingsUpdate(admin_emails=[])
        assert body.admin_emails == []

    def test_resource_owner_upsert_dedupes_emails(self):
        body = ResourceOwnerUpsert(
            emails=[" owner@x.com ", "owner@x.com", "ops@x.com", " "],
        )
        assert body.emails == ["owner@x.com", "ops@x.com"]

    def test_resource_owner_upsert_allows_empty(self):
        body = ResourceOwnerUpsert(emails=[])
        assert body.emails == []

    def test_recipient_test_request_requires_at_least_one_email(self):
        with pytest.raises(Exception):
            RecipientTestRequest(mail_channel_id=1, emails=[" ", ""])

    def test_channel_create_rejects_userinfo_in_webhook_url(self):
        with pytest.raises(Exception):
            AlertChannelCreate(
                name="userinfo-leak",
                webhook_url="https://token:secret@hooks.example.com/path",
                payload_template="{}",
            )

    def test_channel_create_rejects_hostname_that_resolves_private(self, monkeypatch):
        def fake_getaddrinfo(*_args, **_kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.10.5", 443))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        with pytest.raises(Exception):
            AlertChannelCreate(
                name="private-dns",
                webhook_url="https://hooks.example.com/private",
                payload_template="{}",
            )

    def test_alert_status_response(self):
        s = AlertStatusResponse(target="mydb", type="db_health", status="alert", since="2026-04-11T12:00:00")
        assert s.status == "alert"


class TestMaskWebhookUrl:
    def test_strips_userinfo(self):
        from app.routers.alerts import _mask_webhook_url
        masked = _mask_webhook_url("https://token:secret@hooks.example.com/path/X?q=1")
        assert "token" not in masked
        assert "secret" not in masked
        assert masked == "https://hooks.example.com/***"

    def test_preserves_port(self):
        from app.routers.alerts import _mask_webhook_url
        assert _mask_webhook_url("https://hooks.example.com:8443/svc/abc") == "https://hooks.example.com:8443/***"

    def test_strips_path_query_fragment(self):
        from app.routers.alerts import _mask_webhook_url
        assert _mask_webhook_url("https://hooks.example.com/p?q=1#f") == "https://hooks.example.com/***"

    def test_unparseable_url_falls_back_to_stars(self):
        from app.routers.alerts import _mask_webhook_url
        assert _mask_webhook_url("not a url") == "***"


class TestAlertPermissions:
    def test_alerts_read_in_all_permissions(self):
        assert "alerts.read" in ALL_PERMISSIONS

    def test_alerts_write_in_all_permissions(self):
        assert "alerts.write" in ALL_PERMISSIONS

class TestAlertState:
    """N-strike behavior. Defaults to N=2 (matching production default)."""

    def test_initial_state_is_ok(self):
        mgr = AlertStateManager()
        assert mgr.get_status("db_health", "mydb") == "ok"

    def test_n2_cold_start_first_failure_is_silent(self):
        mgr = AlertStateManager()
        transition = mgr.update(
            "db_health", "mydb", is_healthy=False, trigger_after_failures=2,
        )
        assert transition is None
        assert mgr.get_status("db_health", "mydb") == "ok"

    def test_n2_cold_start_second_failure_triggers(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        transition = mgr.update(
            "db_health", "mydb", is_healthy=False, trigger_after_failures=2,
        )
        assert transition == "triggered"
        assert mgr.get_status("db_health", "mydb") == "alert"

    def test_n1_cold_start_first_failure_triggers_immediately(self):
        mgr = AlertStateManager()
        transition = mgr.update(
            "db_health", "mydb", is_healthy=False, trigger_after_failures=1,
        )
        assert transition == "triggered"
        assert mgr.get_status("db_health", "mydb") == "alert"

    def test_already_alert_stays_silent_on_more_failures(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        transition = mgr.update(
            "db_health", "mydb", is_healthy=False, trigger_after_failures=2,
        )
        assert transition is None

    def test_resolved_emitted_on_healthy_after_alert(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        transition = mgr.update(
            "db_health", "mydb", is_healthy=True, trigger_after_failures=2,
        )
        assert transition == "resolved"
        assert mgr.get_status("db_health", "mydb") == "ok"

    def test_healthy_after_unnotified_failure_does_not_resolve(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=2)
        transition = mgr.update(
            "db_health", "mydb", is_healthy=True, trigger_after_failures=2,
        )
        assert transition is None
        assert mgr.get_status("db_health", "mydb") == "ok"

    def test_flap_resets_counter_no_emission(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=3)
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=3)
        # Healthy mid-streak resets counter
        mgr.update("db_health", "mydb", is_healthy=True, trigger_after_failures=3)
        # Now must take 3 more failures to fire
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=3)
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=3)
        transition = mgr.update(
            "db_health", "mydb", is_healthy=False, trigger_after_failures=3,
        )
        assert transition == "triggered"

    def test_no_transition_when_still_ok(self):
        mgr = AlertStateManager()
        transition = mgr.update(
            "db_health", "mydb", is_healthy=True, trigger_after_failures=2,
        )
        assert transition is None

    def test_get_all_alerts(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "db1", is_healthy=False, trigger_after_failures=1)
        mgr.update("upstream_health", "svc1", is_healthy=False, trigger_after_failures=1)
        mgr.update("db_health", "db2", is_healthy=True, trigger_after_failures=1)
        alerts = mgr.get_all_alerts()
        assert len(alerts) == 2
        targets = {a["target"] for a in alerts}
        assert targets == {"db1", "svc1"}

    def test_get_all_statuses_includes_known_ok_and_alert_states(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "db1", is_healthy=False, trigger_after_failures=1)
        mgr.update("db_health", "db2", is_healthy=True, trigger_after_failures=1)
        statuses = mgr.get_all_statuses()
        status_by_target = {entry["target"]: entry["status"] for entry in statuses}
        assert status_by_target == {"db1": "alert", "db2": "ok"}

    def test_reset_clears_all(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=1)
        mgr.reset()
        assert mgr.get_status("db_health", "mydb") == "ok"
        assert mgr.get_all_alerts() == []

    def test_n_lowered_mid_flight_fires_on_next_failure(self):
        mgr = AlertStateManager()
        # Build up to fail_count=2 under N=5
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=5)
        mgr.update("db_health", "mydb", is_healthy=False, trigger_after_failures=5)
        # N tightened to 3; next failure should fire (counter becomes 3)
        transition = mgr.update(
            "db_health", "mydb", is_healthy=False, trigger_after_failures=3,
        )
        assert transition == "triggered"

    def test_discard_removes_only_matching_entry(self):
        mgr = AlertStateManager()
        mgr.update("db_health", "db1", is_healthy=False, trigger_after_failures=1)
        mgr.update("db_health", "db2", is_healthy=False, trigger_after_failures=1)
        mgr.update("upstream_health", "db1", is_healthy=False, trigger_after_failures=1)

        mgr.discard("db_health", "db1")

        assert mgr.get_status("db_health", "db1") == "ok"
        assert mgr.get_status("db_health", "db2") == "alert"
        assert mgr.get_status("upstream_health", "db1") == "alert"

    def test_discard_is_noop_when_missing(self):
        mgr = AlertStateManager()
        mgr.discard("db_health", "never-existed")
        assert mgr.get_all_alerts() == []

    @pytest.mark.asyncio
    async def test_purge_stale_states(self, seeded_db):
        from app.services.alert_state import (
            purge_stale_states,
            save_alert_state_to_db,
        )

        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        mgr = AlertStateManager()
        mgr.update("db_health", "live-db", is_healthy=False, trigger_after_failures=1)
        mgr.update("db_health", "ghost-db", is_healthy=False, trigger_after_failures=1)
        mgr.update("nas_health", "live-nas", is_healthy=False, trigger_after_failures=1)
        mgr.update("nas_health", "ghost-nas", is_healthy=False, trigger_after_failures=1)
        mgr.update("upstream_health", "live-up", is_healthy=False, trigger_after_failures=1)
        mgr.update("upstream_health", "ghost-up", is_healthy=False, trigger_after_failures=1)
        # Route states are now keyed by the plain route_id.
        mgr.update("route_error_rate", "live-route", is_healthy=False, trigger_after_failures=1)
        mgr.update("route_error_rate", "ghost-route", is_healthy=False, trigger_after_failures=1)
        # Legacy leftovers: global error_rate (monitoring removed) and a
        # rule-scoped route target from the old model — both must be purged.
        mgr.update("error_rate", "global:rule_1", is_healthy=False, trigger_after_failures=1)
        mgr.update("route_error_rate", "live-route:rule_99", is_healthy=False, trigger_after_failures=1)

        async with session_factory() as db:
            for atype, target in [
                ("db_health", "live-db"),
                ("db_health", "ghost-db"),
                ("nas_health", "live-nas"),
                ("nas_health", "ghost-nas"),
                ("upstream_health", "live-up"),
                ("upstream_health", "ghost-up"),
                ("route_error_rate", "live-route"),
                ("route_error_rate", "ghost-route"),
                ("error_rate", "global:rule_1"),
                ("route_error_rate", "live-route:rule_99"),
            ]:
                await save_alert_state_to_db(db, mgr, atype, target)

        async with session_factory() as db:
            removed = await purge_stale_states(
                db,
                mgr,
                known_db_aliases={"live-db"},
                known_nas_aliases={"live-nas"},
                known_upstream_ids={"live-up"},
                known_route_ids={"live-route"},
            )

        removed_set = set(removed)
        assert ("db_health", "ghost-db") in removed_set
        assert ("nas_health", "ghost-nas") in removed_set
        assert ("upstream_health", "ghost-up") in removed_set
        assert ("route_error_rate", "ghost-route") in removed_set
        assert ("error_rate", "global:rule_1") in removed_set
        assert ("route_error_rate", "live-route:rule_99") in removed_set
        assert len(removed) == 6

        assert mgr.get_status("db_health", "live-db") == "alert"
        assert mgr.get_status("db_health", "ghost-db") == "ok"
        assert mgr.get_status("nas_health", "live-nas") == "alert"
        assert mgr.get_status("nas_health", "ghost-nas") == "ok"
        assert mgr.get_status("upstream_health", "live-up") == "alert"
        assert mgr.get_status("upstream_health", "ghost-up") == "ok"
        assert mgr.get_status("route_error_rate", "live-route") == "alert"
        assert mgr.get_status("route_error_rate", "ghost-route") == "ok"
        assert mgr.get_status("error_rate", "global:rule_1") == "ok"
        assert mgr.get_status("route_error_rate", "live-route:rule_99") == "ok"

    @pytest.mark.asyncio
    async def test_purge_stale_states_skips_when_apisix_unknown(self, seeded_db):
        from app.services.alert_state import (
            purge_stale_states,
            save_alert_state_to_db,
        )

        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        mgr = AlertStateManager()
        mgr.update("upstream_health", "any-up", is_healthy=False, trigger_after_failures=1)
        mgr.update("route_error_rate", "any-route", is_healthy=False, trigger_after_failures=1)
        async with session_factory() as db:
            await save_alert_state_to_db(db, mgr, "upstream_health", "any-up")
            await save_alert_state_to_db(db, mgr, "route_error_rate", "any-route")

        async with session_factory() as db:
            removed = await purge_stale_states(
                db,
                mgr,
                known_db_aliases=set(),
                known_nas_aliases=set(),
                known_upstream_ids=None,
                known_route_ids=None,
            )
        assert removed == []
        assert mgr.get_status("upstream_health", "any-up") == "alert"
        assert mgr.get_status("route_error_rate", "any-route") == "alert"

    @pytest.mark.asyncio
    async def test_delete_alert_state_removes_persisted_row(self, seeded_db):
        from app.services.alert_state import (
            delete_alert_state,
            save_alert_state_to_db,
        )

        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        mgr = AlertStateManager()
        mgr.update("db_health", "doomed-db", is_healthy=False, trigger_after_failures=1)
        async with session_factory() as db:
            await save_alert_state_to_db(db, mgr, "db_health", "doomed-db")

        async with session_factory() as db:
            await delete_alert_state(db, "db_health", "doomed-db")
            await db.commit()

        restored = AlertStateManager()
        async with session_factory() as db:
            from app.services.alert_state import load_alert_state_from_db
            await load_alert_state_from_db(db, restored)
        assert restored.get_all_statuses() == []

    @pytest.mark.asyncio
    async def test_persist_and_restore_alert_state(self, seeded_db):
        from app.services.alert_state import (
            load_alert_state_from_db,
            save_alert_state_to_db,
        )

        session_factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
        mgr = AlertStateManager()
        mgr.update("db_health", "main-db", is_healthy=False, trigger_after_failures=2)
        mgr.update("db_health", "main-db", is_healthy=False, trigger_after_failures=2)

        async with session_factory() as db:
            await save_alert_state_to_db(db, mgr, "db_health", "main-db")

        restored = AlertStateManager()
        async with session_factory() as db:
            await load_alert_state_from_db(db, restored)

        assert restored.get_status("db_health", "main-db") == "alert"
        statuses = restored.get_all_statuses()
        assert statuses[0]["target"] == "main-db"
        assert statuses[0]["status"] == "alert"
        # fail_count must round-trip
        entry = restored.get_entry("db_health", "main-db")
        assert entry is not None
        assert entry["fail_count"] == 2
