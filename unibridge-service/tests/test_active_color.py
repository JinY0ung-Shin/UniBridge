"""Tests for blue/green active-color detection (app.services.active_color)."""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import active_color
from app.services.active_color import _node_addresses, is_active_instance


@pytest.fixture(autouse=True)
def _reset_transition_log_state():
    active_color._last_logged_active = None
    yield
    active_color._last_logged_active = None


async def test_hard_off_switch_wins(monkeypatch):
    monkeypatch.setattr(settings, "RUN_BACKGROUND_TASKS", False)
    monkeypatch.setattr(settings, "UNIBRIDGE_SELF_NODE", "")
    assert await is_active_instance() is False


async def test_empty_self_node_is_always_active_without_apisix_call(monkeypatch):
    monkeypatch.setattr(settings, "RUN_BACKGROUND_TASKS", True)
    monkeypatch.setattr(settings, "UNIBRIDGE_SELF_NODE", "")

    from app.services import apisix_client

    async def _fail(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("APISIX must not be consulted in single-instance mode")

    monkeypatch.setattr(apisix_client, "get_resource", _fail)
    assert await is_active_instance() is True


async def test_active_when_upstream_points_at_self(monkeypatch):
    monkeypatch.setattr(settings, "RUN_BACKGROUND_TASKS", True)
    monkeypatch.setattr(settings, "UNIBRIDGE_SELF_NODE", "unibridge-service-blue:8000")

    from app.services import apisix_client

    async def _get(kind, rid):
        assert (kind, rid) == ("upstreams", "unibridge-service")
        return {"nodes": {"unibridge-service-blue:8000": 1}}

    monkeypatch.setattr(apisix_client, "get_resource", _get)
    assert await is_active_instance() is True


async def test_standby_when_upstream_points_elsewhere(monkeypatch):
    monkeypatch.setattr(settings, "RUN_BACKGROUND_TASKS", True)
    monkeypatch.setattr(settings, "UNIBRIDGE_SELF_NODE", "unibridge-service-blue:8000")

    from app.services import apisix_client

    async def _get(kind, rid):
        return {"nodes": {"unibridge-service-green:8000": 1}}

    monkeypatch.setattr(apisix_client, "get_resource", _get)
    assert await is_active_instance() is False


async def test_list_form_nodes_and_zero_weight(monkeypatch):
    monkeypatch.setattr(settings, "RUN_BACKGROUND_TASKS", True)
    monkeypatch.setattr(settings, "UNIBRIDGE_SELF_NODE", "unibridge-service-blue:8000")

    from app.services import apisix_client

    async def _get(kind, rid):
        return {
            "nodes": [
                {"host": "unibridge-service-blue", "port": 8000, "weight": 0},
                {"host": "unibridge-service-green", "port": 8000, "weight": 1},
            ]
        }

    monkeypatch.setattr(apisix_client, "get_resource", _get)
    assert await is_active_instance() is False


async def test_fails_open_when_apisix_unreachable(monkeypatch):
    monkeypatch.setattr(settings, "RUN_BACKGROUND_TASKS", True)
    monkeypatch.setattr(settings, "UNIBRIDGE_SELF_NODE", "unibridge-service-blue:8000")

    from app.services import apisix_client

    async def _boom(*args, **kwargs):
        raise ConnectionError("apisix down")

    monkeypatch.setattr(apisix_client, "get_resource", _boom)
    assert await is_active_instance() is True


def test_node_addresses_normalization():
    assert _node_addresses({"a:1": 1, "b:2": 0}) == {"a:1"}
    assert _node_addresses(
        [
            {"host": "h", "port": 9, "weight": 2},
            {"host": "nop", "port": 9, "weight": 0},
            {"host": "noport", "weight": 1},
            "garbage",
        ]
    ) == {"h:9", "noport"}
    assert _node_addresses(None) == set()
    assert _node_addresses("weird") == set()
