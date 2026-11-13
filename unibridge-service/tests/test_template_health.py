"""Query template samples: stored with the template, run on save, checked periodically."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.middleware.rate_limiter import rate_limiter
from app.models import AlertState, AuditLog, QueryTemplate
from app.routers import alerts as alerts_router
from app.schemas import QueryResponse
from app.services import alert_checker
from app.services.alert_mutes import MuteIndex
from app.services.alert_state import AlertStateManager
from tests.conftest import auth_header
from tests.test_query_templates import _create_api_key, _create_database

RULE = "template_health"
SQL = "SELECT id, name FROM users WHERE id = :id"


@pytest.fixture(autouse=True)
def _fresh_rate_limiter():
    """A save-time sample run is the caller's own query and counts against
    their per-minute limit; the limiter is process-wide, so in a full run the
    shared test administrator would arrive here with part of it spent."""
    with rate_limiter._lock:
        rate_limiter._requests.clear()
    yield
    with rate_limiter._lock:
        rate_limiter._requests.clear()


def _rows(count: int) -> QueryResponse:
    return QueryResponse(
        columns=["id", "name"],
        rows=[[1, "alice"]][:count],
        row_count=count,
        truncated=False,
        elapsed_ms=4,
    )


@contextmanager
def _connected(result: QueryResponse | Exception | None = None, db_type: str = "postgres"):
    """The template's database is connected and answers with ``result``."""
    answer = _rows(1) if result is None else result
    with patch(
        "app.routers.query.connection_manager.get_db_type", return_value=db_type
    ), patch(
        "app.routers.query.connection_manager.get_engine", return_value=MagicMock()
    ), patch(
        "app.routers.query.connection_manager.update_pool_metrics"
    ), patch(
        "app.routers.query.execute_query",
        new_callable=AsyncMock,
        **({"side_effect": answer} if isinstance(answer, Exception) else {"return_value": answer}),
    ) as execute_query:
        yield execute_query


async def _post_template(client, admin_token, **overrides):
    body = {
        "path": "reports/users",
        "name": "Users report",
        "database": "maindb",
        "sql": SQL,
        **overrides,
    }
    return await client.post(
        "/admin/query/templates", json=body, headers=auth_header(admin_token)
    )


async def _stored(seeded_db, path: str = "reports/users") -> QueryTemplate | None:
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        result = await db.execute(select(QueryTemplate).where(QueryTemplate.path == path))
        return result.scalar_one_or_none()


# ── Saving a sample ─────────────────────────────────────────────────────────


async def test_sample_is_run_once_and_saved_with_the_template(client, admin_token, seeded_db):
    await _create_database(client, admin_token)
    with _connected() as execute_query:
        resp = await _post_template(client, admin_token, sample_params={"id": 1}, timeout=20)
    assert resp.status_code == 201, resp.text
    assert resp.json()["sample_params"] == {"id": 1}
    assert resp.json()["monitor_enabled"] is True
    kwargs = execute_query.await_args.kwargs
    assert kwargs["params"] == {"id": 1}
    assert kwargs["limit"] == 1
    assert kwargs["timeout"] == 20
    assert json.loads((await _stored(seeded_db)).sample_params) == {"id": 1}

    listed = await client.get("/admin/query/templates", headers=auth_header(admin_token))
    assert listed.json()[0]["sample_params"] == {"id": 1}


async def test_sample_names_must_be_the_template_params(client, admin_token):
    await _create_database(client, admin_token)
    with _connected() as execute_query:
        missing = await _post_template(client, admin_token, sample_params={})
        unknown = await _post_template(client, admin_token, sample_params={"id": 1, "x": 2})
    assert missing.status_code == 400
    assert missing.json()["detail"] == "sample_params is missing a value for: id"
    assert unknown.status_code == 400
    assert "does not use: x" in unknown.json()["detail"]
    execute_query.assert_not_awaited()


async def test_template_without_params_takes_an_empty_sample(client, admin_token):
    await _create_database(client, admin_token)
    with _connected() as execute_query:
        resp = await _post_template(client, admin_token, sql="SELECT 1", sample_params={})
    assert resp.status_code == 201
    assert resp.json()["sample_params"] == {}
    assert execute_query.await_args.kwargs["params"] is None


async def test_sample_that_returns_no_rows_is_refused(client, admin_token, seeded_db):
    await _create_database(client, admin_token)
    with _connected(_rows(0)):
        resp = await _post_template(client, admin_token, sample_params={"id": 999})
    assert resp.status_code == 400
    assert "returned no rows" in resp.json()["detail"]
    assert await _stored(seeded_db) is None


async def test_sample_that_fails_is_refused_and_lands_in_the_query_history(
    client, admin_token, seeded_db
):
    await _create_database(client, admin_token)
    error = RuntimeError('column "nope" does not exist')
    with _connected(error):
        resp = await _post_template(client, admin_token, sample_params={"id": 1})
    assert resp.status_code == 400
    # The same sentence a failed run gets anywhere; the database's own words
    # are in the query history, like those of any other failed query.
    assert resp.json()["detail"] == (
        "sample_params could not be run: Query execution failed. Check server logs for details."
    )
    assert await _stored(seeded_db) is None
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        row = (await db.execute(select(AuditLog))).scalar_one()
    assert row.status == "error" and 'column "nope" does not exist' in row.error_message
    assert row.user == "testadmin" and row.template_path is None


async def test_sample_is_saved_unrun_when_the_database_is_not_connected(client, admin_token):
    # The alias is registered in the meta DB but this process holds no
    # connection: the save goes through and the periodic check reports later.
    await _create_database(client, admin_token)
    with patch("app.routers.query.execute_query", new_callable=AsyncMock) as execute_query:
        resp = await _post_template(client, admin_token, sample_params={"id": 1})
    assert resp.status_code == 201
    execute_query.assert_not_awaited()


async def test_admin_update_checks_the_sample_against_new_sql(client, admin_token, seeded_db):
    await _create_database(client, admin_token)
    headers = auth_header(admin_token)
    with _connected() as execute_query:
        await _post_template(client, admin_token, sample_params={"id": 1})
        execute_query.reset_mock()

        # Other fields do not run the sample again.
        resp = await client.put(
            "/admin/query/templates/reports/users", json={"description": "x"}, headers=headers
        )
        assert resp.status_code == 200
        execute_query.assert_not_awaited()

        # Nor does sending the whole template back unchanged, as the page does:
        # a description stays editable while the sample finds nothing.
        execute_query.return_value = _rows(0)
        resp = await client.put(
            "/admin/query/templates/reports/users",
            json={"description": "y", "sql": SQL, "database": "maindb", "sample_params": {"id": 1}},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        execute_query.assert_not_awaited()
        execute_query.return_value = _rows(1)

        # New SQL with another parameter: the stored sample no longer fits.
        resp = await client.put(
            "/admin/query/templates/reports/users",
            json={"sql": "SELECT id FROM users WHERE name = :name"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "missing a value for: name" in resp.json()["detail"]

        resp = await client.put(
            "/admin/query/templates/reports/users",
            json={"sql": "SELECT id FROM users WHERE name = :name", "sample_params": {"name": "a"}},
            headers=headers,
        )
        assert resp.status_code == 200
        assert execute_query.await_args.kwargs["params"] == {"name": "a"}
        assert resp.json()["sample_params"] == {"name": "a"}

        # monitor_enabled keeps the sample; null removes it.
        resp = await client.put(
            "/admin/query/templates/reports/users", json={"monitor_enabled": False}, headers=headers
        )
        assert resp.json()["monitor_enabled"] is False
        assert resp.json()["sample_params"] == {"name": "a"}
        resp = await client.put(
            "/admin/query/templates/reports/users", json={"sample_params": None}, headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["sample_params"] is None
    assert (await _stored(seeded_db)).sample_params is None


async def test_agent_creates_and_edits_a_sample(client, admin_token, seeded_db):
    await _create_database(client, admin_token)
    await _create_api_key(
        seeded_db, name="agent", allowed_databases=["maindb"],
        allowed_routes=["query-api", "query-template-write-api"],
    )
    headers = {"X-Consumer-Username": "agent"}
    with _connected() as execute_query:
        created = await client.put(
            "/query/templates/agent/users",
            json={"name": "Users", "database": "maindb", "sql": SQL, "sample_params": {"id": 1}},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        assert created.json()["sample_params"] == {"id": 1}

        listed = await client.get("/query/templates", headers=headers)
        assert listed.json()[0]["sample_params"] == {"id": 1}

        edited = await client.patch(
            "/query/templates/agent/users",
            json={"sample_params": {"id": 2}, "expected_updated_at": created.json()["updated_at"]},
            headers=headers,
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["sample_params"] == {"id": 2}
        assert execute_query.await_args.kwargs["params"] == {"id": 2}

        removed = await client.patch(
            "/query/templates/agent/users", json={"sample_params": None}, headers=headers
        )
        assert removed.status_code == 200
        assert removed.json()["sample_params"] is None

        # Whether the sample is monitored stays an administrator's switch.
        refused = await client.patch(
            "/query/templates/agent/users", json={"monitor_enabled": False}, headers=headers
        )
        assert refused.status_code == 422


async def test_a_key_that_may_not_query_cannot_attach_a_sample(client, admin_token, seeded_db):
    # The save-time run is the caller's own query. Without it, a key holding
    # only the write route could have the service run SQL for it and read
    # "rows / no rows" off the answer, unaudited.
    await _create_database(client, admin_token)
    await _create_api_key(
        seeded_db, name="writer", allowed_databases=["maindb"],
        allowed_routes=["query-template-write-api"],
    )
    headers = {"X-Consumer-Username": "writer"}
    body = {"name": "Users", "database": "maindb", "sql": SQL}
    with _connected() as execute_query:
        refused = await client.put(
            "/query/templates/agent/users", json={**body, "sample_params": {"id": 1}},
            headers=headers,
        )
        assert refused.status_code == 403
        assert refused.json()["detail"] == (
            "sample_params could not be run: Required API key route: query-api"
        )
        created = await client.put("/query/templates/agent/users", json=body, headers=headers)
        assert created.status_code == 201
        patched = await client.patch(
            "/query/templates/agent/users", json={"sample_params": {"id": 1}}, headers=headers
        )
        assert patched.status_code == 403
        execute_query.assert_not_awaited()


async def test_agent_sample_must_return_rows_and_a_stale_edit_runs_nothing(
    client, admin_token, seeded_db
):
    await _create_database(client, admin_token)
    await _create_api_key(
        seeded_db, name="agent", allowed_databases=["maindb"],
        allowed_routes=["query-api", "query-template-write-api"],
    )
    headers = {"X-Consumer-Username": "agent"}
    body = {"name": "Users", "database": "maindb", "sql": SQL, "sample_params": {"id": 1}}
    with _connected(RuntimeError("relation secret_table does not exist")):
        resp = await client.put("/query/templates/agent/users", json=body, headers=headers)
    assert resp.status_code == 400
    assert "secret_table" not in resp.json()["detail"]
    with _connected(_rows(0)):
        resp = await client.put("/query/templates/agent/users", json=body, headers=headers)
    assert resp.status_code == 400
    assert "returned no rows" in resp.json()["detail"]

    with _connected() as execute_query:
        created = await client.put("/query/templates/agent/users", json=body, headers=headers)
        assert created.status_code == 201
        execute_query.reset_mock()
        stale = await client.patch(
            "/query/templates/agent/users",
            json={"sample_params": {"id": 2}, "expected_updated_at": "2000-01-01T00:00:00Z"},
            headers=headers,
        )
        assert stale.status_code == 409
        execute_query.assert_not_awaited()


async def test_agent_sql_edit_needs_a_fitting_sample(client, admin_token, seeded_db):
    await _create_database(client, admin_token)
    await _create_api_key(
        seeded_db, name="agent", allowed_databases=["maindb"],
        allowed_routes=["query-api", "query-template-write-api"],
    )
    headers = {"X-Consumer-Username": "agent"}
    with _connected():
        await _post_template(client, admin_token, sample_params={"id": 1})
        stale = await client.patch(
            "/query/templates/reports/users",
            json={"sql": "SELECT id FROM users WHERE name = :name"},
            headers=headers,
        )
        fitting = await client.patch(
            "/query/templates/reports/users",
            json={"sql": "SELECT id FROM users WHERE name = :name", "sample_params": {"name": "a"}},
            headers=headers,
        )
    assert stale.status_code == 400
    assert "missing a value for: name" in stale.json()["detail"]
    assert fitting.status_code == 200
    assert fitting.json()["sample_params"] == {"name": "a"}


# ── The periodic check ──────────────────────────────────────────────────────


@pytest.fixture
def checker(seeded_db, monkeypatch):
    """The checker on the test database, with mail captured instead of sent."""
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(alert_checker, "async_session", factory)
    # load_mute_index imports the session factory when it runs.
    monkeypatch.setattr("app.database.async_session", factory)
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(alert_checker, "dispatch_alert", dispatch)
    state = AlertStateManager()
    monkeypatch.setattr(alerts_router, "_alert_state", state)
    return state, dispatch


async def _check(state, trigger_after_failures: int = 2) -> None:
    await alert_checker.run_template_checks(
        state, trigger_after_failures=trigger_after_failures, mutes=MuteIndex()
    )


async def _health(client, admin_token) -> dict:
    resp = await client.get("/admin/query/template-health", headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_check_runs_the_sample_and_records_ok(client, admin_token, seeded_db, checker):
    state, dispatch = checker
    await _create_database(client, admin_token)
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)

    async def _audit_rows() -> int:
        async with factory() as db:
            return len((await db.execute(select(AuditLog))).scalars().all())

    with _connected() as execute_query:
        await _post_template(client, admin_token, sample_params={"id": 1})
        # The save-time run was the administrator's own query, and is recorded.
        assert await _audit_rows() == 1
        execute_query.reset_mock()
        await _check(state)
        assert execute_query.await_args.kwargs["params"] == {"id": 1}
        assert execute_query.await_args.kwargs["limit"] == 1
    dispatch.assert_not_awaited()

    health = await _health(client, admin_token)
    assert health["interval_minutes"] == 1440
    entry = health["templates"]["reports/users"]
    assert entry["level"] == "ok" and entry["alerting"] is False
    assert entry["row_count"] == 1 and entry["checked_at"]

    # The periodic checks are nobody's usage: no query audit row, no run count.
    assert await _audit_rows() == 1
    usage = await client.get("/admin/query/template-usage", headers=auth_header(admin_token))
    assert usage.json()["templates"] == {}


async def test_no_rows_warns_then_an_error_escalates_then_rows_resolve(
    client, admin_token, checker
):
    state, dispatch = checker
    await _create_database(client, admin_token)
    with _connected():
        await _post_template(client, admin_token, sample_params={"id": 1})

    # Two rounds without rows open a warning (trigger_after_failures=2).
    with _connected(_rows(0)):
        await _check(state)
        dispatch.assert_not_awaited()
        assert (await _health(client, admin_token))["templates"]["reports/users"]["level"] == "warning"
        await _check(state)
    kwargs = dispatch.await_args.kwargs
    assert kwargs["alert_type"] == "triggered" and kwargs["rule_type"] == RULE
    assert kwargs["severity"] == "warning"
    assert (kwargs["resource_type"], kwargs["resource_id"]) == ("template", "reports/users")
    assert kwargs["target"] == "reports/users"
    assert kwargs["display_target"] == "Users report (reports/users)"
    # No assignees of its own: the database's owners are told.
    assert kwargs["assignee_refs"] == [("db", "maindb")]
    assert "returned no rows" in kwargs["message"]
    health = (await _health(client, admin_token))["templates"]["reports/users"]
    assert health["alerting"] is True and health["since"] and health["reason"] == "no_rows"

    # A run that fails outright is worse: announced again, as critical.
    dispatch.reset_mock()
    with _connected(RuntimeError('relation "users" does not exist\n[SQL: ...]')):
        await _check(state)
    kwargs = dispatch.await_args.kwargs
    assert kwargs["alert_type"] == "triggered" and kwargs["severity"] == "critical"
    assert 'relation "users" does not exist' in kwargs["message"]
    assert "[SQL" not in kwargs["message"]
    health = (await _health(client, admin_token))["templates"]["reports/users"]
    assert health["level"] == "error" and health["reason"] == "query_error"
    assert health["error"] == 'relation "users" does not exist'

    # One run with rows resolves it: recovery is not damped.
    dispatch.reset_mock()
    with _connected():
        await _check(state)
    assert dispatch.await_args.kwargs["alert_type"] == "resolved"
    assert state.get_status(RULE, "reports/users") == "ok"


async def test_timeout_and_blocked_sql_are_errors(client, admin_token, checker):
    import asyncio

    state, dispatch = checker
    await _create_database(client, admin_token)
    with _connected():
        await _post_template(client, admin_token, sample_params={"id": 1})
    with _connected(asyncio.TimeoutError()):
        await _check(state, trigger_after_failures=1)
    assert dispatch.await_args.kwargs["severity"] == "critical"
    assert "timed out" in dispatch.await_args.kwargs["message"]

    # An administrator's later deny-list entry breaks the template for callers too.
    with _connected() as execute_query, patch(
        "app.routers.query.settings_manager"
    ) as settings_manager:
        settings_manager.blocked_sql_keywords = ["users"]
        await _check(state, trigger_after_failures=1)
        execute_query.assert_not_awaited()
    health = (await _health(client, admin_token))["templates"]["reports/users"]
    assert health["level"] == "error" and health["reason"] == "blocked"


async def test_templates_of_a_down_or_unconnected_database_are_skipped(
    client, admin_token, checker
):
    state, dispatch = checker
    await _create_database(client, admin_token)
    with _connected():
        await _post_template(client, admin_token, sample_params={"id": 1})

    # Not connected in this process: nothing to ask.
    with patch("app.routers.query.execute_query", new_callable=AsyncMock) as execute_query:
        await _check(state)
        execute_query.assert_not_awaited()

    # The database has its own open alert: one mail, not one per template.
    state.update("db_health", "maindb", is_healthy=False, trigger_after_failures=1)
    with _connected(RuntimeError("connection refused")) as execute_query:
        await _check(state, trigger_after_failures=1)
        execute_query.assert_not_awaited()
    dispatch.assert_not_awaited()
    assert state.get_entry(RULE, "reports/users") is None


async def test_only_enabled_monitored_templates_with_a_sample_are_checked(
    client, admin_token, checker
):
    state, _dispatch = checker
    await _create_database(client, admin_token)
    with _connected() as execute_query:
        await _post_template(client, admin_token, path="a/checked", sample_params={"id": 1})
        await _post_template(client, admin_token, path="b/no-sample")
        await _post_template(
            client, admin_token, path="c/unmonitored", sample_params={"id": 1},
            monitor_enabled=False,
        )
        await _post_template(
            client, admin_token, path="d/disabled", sample_params={"id": 1}, enabled=False
        )
        execute_query.reset_mock()
        await _check(state)
        assert execute_query.await_count == 1
    assert set((await _health(client, admin_token))["templates"]) == {"a/checked"}


async def test_state_is_forgotten_when_a_template_stops_being_checked(
    client, admin_token, seeded_db, checker
):
    state, dispatch = checker
    await _create_database(client, admin_token)
    headers = auth_header(admin_token)
    with _connected():
        await _post_template(client, admin_token, path="a/one", sample_params={"id": 1})
        await _post_template(client, admin_token, path="b/two", sample_params={"id": 1})
    with _connected(_rows(0)):
        await _check(state, trigger_after_failures=1)
    assert state.get_status(RULE, "a/one") == "alert"

    # Deleted, or monitoring switched off: the next round closes the open
    # alerts, telling the people who were told they had opened.
    resp = await client.delete("/admin/query/templates/a/one", headers=headers)
    assert resp.status_code == 204
    await client.put(
        "/admin/query/templates/b/two", json={"monitor_enabled": False}, headers=headers
    )
    dispatch.reset_mock()
    with _connected(_rows(0)):
        await _check(state, trigger_after_failures=1)
    assert state.get_entry(RULE, "a/one") is None
    assert state.get_entry(RULE, "b/two") is None
    closed = {call.kwargs["target"]: call.kwargs for call in dispatch.await_args_list}
    assert set(closed) == {"a/one", "b/two"}
    assert closed["a/one"]["alert_type"] == "resolved"
    assert "no longer checked" in closed["a/one"]["message"]
    assert closed["a/one"]["assignee_refs"] == [("db", "maindb")]
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        assert (await db.execute(select(AlertState))).scalars().all() == []
    assert (await _health(client, admin_token))["templates"] == {}


async def test_a_muted_template_alert_is_withheld(client, admin_token, checker):
    from datetime import datetime, timedelta, timezone

    state, dispatch = checker
    await _create_database(client, admin_token)
    with _connected():
        await _post_template(client, admin_token, sample_params={"id": 1})
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    resp = await client.put(
        "/admin/alerts/mutes",
        json={"resource_type": "template", "resource_id": "reports/users", "muted_until": until},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text
    with _connected(_rows(0)):
        await alert_checker.run_template_checks(state, trigger_after_failures=1)
    dispatch.assert_not_awaited()
    assert state.get_status(RULE, "reports/users") == "alert"

    status = await client.get("/admin/alerts/status", headers=auth_header(admin_token))
    item = next(i for i in status.json()["items"] if i["type"] == RULE)
    assert (item["resource_type"], item["resource_id"]) == ("template", "reports/users")
    assert item["muted"] is True and item["severity"] == "warning"
    # Judged by the template cadence (a day), not the 60s health cycle.
    assert item["stale"] is False and item["check_interval_seconds"] == 86400
    assert item["resolve_after_successes"] == 1


KST = timezone(timedelta(hours=9))
# A Monday far enough ahead that every template here was last edited before it.
MONDAY_8 = datetime(2030, 3, 4, 8, 0, tzinfo=KST)


def _checked_at(state, path: str, when: datetime) -> None:
    """Pretend the template's last check was at ``when``."""
    state._states[(RULE, path)]["details"]["current"]["checked_at"] = when.isoformat()


async def _round(state, now: datetime | None = None) -> None:
    await alert_checker.run_template_checks(
        state, trigger_after_failures=2, mutes=MuteIndex(), interval_minutes=1440, now=now
    )


async def test_the_daily_run_is_at_8_in_the_morning_kst(client, admin_token, checker):
    state, _dispatch = checker
    await _create_database(client, admin_token)
    with _connected() as execute_query:
        await _post_template(client, admin_token, sample_params={"id": 1})
        execute_query.reset_mock()

        await _round(state)  # never checked: due at once, not tomorrow morning
        assert execute_query.await_count == 1

        # Checked this morning: nothing more today, whatever the hour.
        _checked_at(state, "reports/users", MONDAY_8 + timedelta(minutes=1))
        await _round(state, MONDAY_8 + timedelta(hours=15))
        await _round(state, MONDAY_8 + timedelta(hours=23, minutes=59))
        assert execute_query.await_count == 1
        await _round(state, MONDAY_8 + timedelta(days=1))
        assert execute_query.await_count == 2

        # Checked late (a save at 17:00): tomorrow 08:00 all the same, not 17:00.
        _checked_at(state, "reports/users", MONDAY_8 + timedelta(hours=9))
        await _round(state, MONDAY_8 + timedelta(days=1))
        assert execute_query.await_count == 3

        # A sample added later does not wait for the morning.
        await _post_template(client, admin_token, path="reports/new", sample_params={"id": 1})
        execute_query.reset_mock()
        await _round(state)
        assert execute_query.await_count == 1


async def test_a_failure_is_confirmed_in_minutes_and_an_open_alert_is_rechecked_half_hourly(
    client, admin_token, checker
):
    state, dispatch = checker
    await _create_database(client, admin_token)
    with _connected():
        await _post_template(client, admin_token, sample_params={"id": 1})

    # One failure is not an alert yet; it is confirmed ten minutes later, so
    # the mail goes out this morning and not after tomorrow's run.
    with _connected(_rows(0)) as execute_query:
        await _round(state)
        assert execute_query.await_count == 1
        dispatch.assert_not_awaited()
        _checked_at(state, "reports/users", MONDAY_8)
        await _round(state, MONDAY_8 + timedelta(minutes=9))
        assert execute_query.await_count == 1
        await _round(state, MONDAY_8 + timedelta(minutes=10))
        assert execute_query.await_count == 2
    assert dispatch.await_args.kwargs["alert_type"] == "triggered"

    # With the alert open it is run every 30 minutes, and nothing is mailed
    # while it keeps failing the same way.
    dispatch.reset_mock()
    with _connected(_rows(0)) as execute_query:
        _checked_at(state, "reports/users", MONDAY_8 + timedelta(minutes=10))
        await _round(state, MONDAY_8 + timedelta(minutes=39))
        execute_query.assert_not_awaited()
        await _round(state, MONDAY_8 + timedelta(minutes=40))
        assert execute_query.await_count == 1
    dispatch.assert_not_awaited()

    # The first of those runs that returns rows closes it.
    with _connected() as execute_query:
        _checked_at(state, "reports/users", MONDAY_8 + timedelta(minutes=40))
        await _round(state, MONDAY_8 + timedelta(minutes=69))
        execute_query.assert_not_awaited()
        await _round(state, MONDAY_8 + timedelta(minutes=70))
        assert execute_query.await_count == 1
    assert dispatch.await_args.kwargs["alert_type"] == "resolved"

    # Healthy again: back to the morning run only.
    with _connected() as execute_query:
        _checked_at(state, "reports/users", MONDAY_8 + timedelta(minutes=70))
        await _round(state, MONDAY_8 + timedelta(hours=23, minutes=59))
        execute_query.assert_not_awaited()


async def test_an_edited_template_is_checked_again_at_once(client, admin_token, checker):
    state, dispatch = checker
    await _create_database(client, admin_token)
    with _connected():
        await _post_template(client, admin_token, sample_params={"id": 1})
    with _connected(RuntimeError("relation users does not exist")):
        await alert_checker.run_template_checks(
            state, trigger_after_failures=1, mutes=MuteIndex(), interval_minutes=1440
        )
    assert state.get_status(RULE, "reports/users") == "alert"

    # Somebody fixes the SQL: the next cycle sees the edit and closes the
    # alert, instead of leaving it open until the morning.
    dispatch.reset_mock()
    with _connected() as execute_query:
        await _round(state)
        execute_query.assert_not_awaited()
        resp = await client.put(
            "/admin/query/templates/reports/users",
            json={"sql": "SELECT id, name FROM people WHERE id = :id"},
            headers=auth_header(admin_token),
        )
        assert resp.status_code == 200, resp.text
        execute_query.reset_mock()
        await _round(state)
        assert execute_query.await_count == 1
    assert dispatch.await_args.kwargs["alert_type"] == "resolved"


def test_the_interval_is_laid_out_from_8_kst():
    def _due(checked: datetime, now: datetime, interval: int, **entry) -> bool:
        return alert_checker._template_check_due(
            {"status": "ok", "fail_count": 0, **entry,
             "details": {"current": {"checked_at": checked.isoformat()}}},
            interval, now,
        )

    # 12 hours: 08:00 and 20:00 KST.
    assert _due(MONDAY_8 + timedelta(hours=1), MONDAY_8 + timedelta(hours=11), 720) is False
    assert _due(MONDAY_8 + timedelta(hours=1), MONDAY_8 + timedelta(hours=12), 720) is True
    # A week: Monday 08:00 KST.
    assert _due(MONDAY_8, MONDAY_8 + timedelta(days=6, hours=23), 10080) is False
    assert _due(MONDAY_8, MONDAY_8 + timedelta(days=7), 10080) is True
    # The state stores UTC; the morning is still KST's (23:00 UTC the day before).
    utc_check = (MONDAY_8 + timedelta(hours=2)).astimezone(timezone.utc)
    assert _due(utc_check, datetime(2030, 3, 4, 22, 59, tzinfo=timezone.utc), 1440) is False
    assert _due(utc_check, datetime(2030, 3, 4, 23, 0, tzinfo=timezone.utc), 1440) is True
    # An open alert is looked at every 30 minutes; a short interval is never stretched.
    assert _due(MONDAY_8, MONDAY_8 + timedelta(minutes=29), 1440, status="alert") is False
    assert _due(MONDAY_8, MONDAY_8 + timedelta(minutes=30), 1440, status="alert") is True
    assert _due(MONDAY_8, MONDAY_8 + timedelta(minutes=5), 5, fail_count=1) is True
    assert _due(MONDAY_8, MONDAY_8 + timedelta(minutes=5), 5, status="alert") is True


async def test_the_cycle_starts_a_round_unless_one_is_running_or_the_check_is_off(
    client, admin_token, checker, monkeypatch
):
    state, _dispatch = checker
    rounds = AsyncMock()
    monkeypatch.setattr(alert_checker, "run_template_checks", rounds)
    monkeypatch.setattr(alert_checker, "_template_round_task", None)

    await alert_checker._maybe_start_template_checks(state, 2)
    await alert_checker._template_round_task
    assert rounds.await_args.kwargs["interval_minutes"] == 1440

    # A round still in flight is not doubled.
    import asyncio

    blocker = asyncio.create_task(asyncio.sleep(3600))
    monkeypatch.setattr(alert_checker, "_template_round_task", blocker)
    await alert_checker._maybe_start_template_checks(state, 2)
    assert alert_checker._template_round_task is blocker
    assert rounds.await_count == 1
    blocker.cancel()
    monkeypatch.setattr(alert_checker, "_template_round_task", None)

    # 0 switches the check off and closes what it had open.
    resp = await client.put(
        "/admin/alerts/settings",
        json={"template_check_interval_minutes": 0},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["template_check_interval_minutes"] == 0
    state.update(RULE, "reports/users", is_healthy=False, trigger_after_failures=1)
    await alert_checker._maybe_start_template_checks(state, 2)
    assert rounds.await_count == 1
    assert state.get_entry(RULE, "reports/users") is None
    assert (await _health(client, admin_token))["interval_minutes"] == 0

    weekly = await client.put(
        "/admin/alerts/settings",
        json={"template_check_interval_minutes": 10080},
        headers=auth_header(admin_token),
    )
    assert weekly.status_code == 200
    too_long = await client.put(
        "/admin/alerts/settings",
        json={"template_check_interval_minutes": 10081},
        headers=auth_header(admin_token),
    )
    assert too_long.status_code == 422


async def test_an_unreadable_interval_skips_the_cycle(monkeypatch):
    # No alert_settings table behind this session factory: neither the
    # templates nor an "off" can be read, so nothing is started.
    class _Broken:
        async def __aenter__(self):
            raise RuntimeError("meta database is down")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(alert_checker, "async_session", lambda: _Broken())
    monkeypatch.setattr(alert_checker, "_template_round_task", None)
    rounds = AsyncMock()
    monkeypatch.setattr(alert_checker, "run_template_checks", rounds)
    await alert_checker._maybe_start_template_checks(AlertStateManager(), 2)
    assert alert_checker._template_round_task is None
    rounds.assert_not_awaited()


# ── A real engine, no mocked executor ───────────────────────────────────────


async def test_sample_run_against_a_real_engine():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.routers.query import run_template_sample

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE users (id INTEGER, name TEXT)"))
        await conn.execute(text("INSERT INTO users VALUES (1, 'alice'), (2, 'bob')"))

    async def _run(sql: str, sample: dict):
        with patch(
            "app.routers.query.connection_manager.get_engine", return_value=engine
        ), patch("app.routers.query.connection_manager.update_pool_metrics"):
            return await run_template_sample(
                database="maindb", db_type="postgres", sql=sql, sample_params=sample, timeout=10,
            )

    try:
        found = await _run(SQL, {"id": 1})
        assert (found.level, found.reason, found.row_count) == ("ok", None, 1)

        # Many rows match, one is asked for.
        many = await _run("SELECT id FROM users", {})
        assert (many.level, many.row_count) == ("ok", 1)

        empty = await _run(SQL, {"id": 999})
        assert (empty.level, empty.reason, empty.row_count) == ("warning", "no_rows", 0)

        broken = await _run("SELECT id FROM dropped_table WHERE id = :id", {"id": 1})
        assert (broken.level, broken.reason) == ("error", "query_error")
        assert "dropped_table" in broken.error
        assert "[SQL" not in broken.error and "\n" not in broken.error

        writing = await _run("DELETE FROM users WHERE id = :id", {"id": 1})
        assert (writing.level, writing.reason) == ("error", "blocked")
        still_there = await _run("SELECT id FROM users", {})
        assert still_there.level == "ok"
    finally:
        await engine.dispose()


# ── Review follow-ups ───────────────────────────────────────────────────────


async def test_a_long_name_and_path_still_fit_the_state_row(client, admin_token, seeded_db, checker):
    # display_target is String(200); "name (path)" can reach 303 characters,
    # which Postgres refuses, so neither state nor history would be saved.
    state, _dispatch = checker
    await _create_database(client, admin_token)
    path = "reports/" + "p" * 150
    with _connected():
        resp = await _post_template(
            client, admin_token, path=path, name="n" * 90, sample_params={"id": 1}
        )
        assert resp.status_code == 201, resp.text
        await _check(state)
    entry = state.get_entry(RULE, path)
    assert entry["display_target"] == path
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        row = (await db.execute(select(AlertState))).scalar_one()
    assert len(row.display_target) <= 200


async def test_a_graph_result_counts_as_data():
    from app.routers.query import run_template_sample

    # CONSTRUCT/DESCRIBE answer with a graph and row_count 0.
    graph = QueryResponse(
        columns=[], rows=[], row_count=0, truncated=False, elapsed_ms=3,
        graph="<a> <b> <c> .",
    )
    empty = QueryResponse(columns=[], rows=[], row_count=0, truncated=False, elapsed_ms=3, graph="")
    sparql = "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"
    for response, level in ((graph, "ok"), (empty, "warning")):
        with patch(
            "app.routers.query.connection_manager.get_graphdb_client", return_value=MagicMock()
        ), patch(
            "app.routers.query.connection_manager.get_database_name", return_value="repo"
        ), patch("app.routers.query.connection_manager.update_pool_metrics"), patch(
            "app.routers.query.execute_graphdb_query",
            new_callable=AsyncMock,
            return_value=response,
        ):
            result = await run_template_sample(
                database="kg", db_type="graphdb", sql=sparql, sample_params={}, timeout=None,
            )
        assert result.level == level


async def test_a_demoted_color_stops_its_round(monkeypatch):
    import asyncio

    started = asyncio.Event()

    async def _slow_round(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(_slow_round())
    monkeypatch.setattr(alert_checker, "_template_round_task", task)
    await started.wait()
    await alert_checker.stop_template_checks()
    assert task.cancelled()
