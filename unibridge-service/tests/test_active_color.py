"""Tests for blue/green active-color detection (app.services.active_color)."""
from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from app.config import settings
from app.services import active_color
from app.services.active_color import is_active_instance
from app.services.apisix_client import upstream_node_addresses


@pytest.fixture(autouse=True)
def _reset_transition_log_state():
    active_color._last_logged_active = None
    yield
    active_color._last_logged_active = None


def _gauge() -> float | None:
    return REGISTRY.get_sample_value("unibridge_active_instance")


async def test_hard_off_switch_wins(monkeypatch):
    monkeypatch.setattr(settings, "RUN_BACKGROUND_TASKS", False)
    monkeypatch.setattr(settings, "UNIBRIDGE_SELF_NODE", "")
    assert await is_active_instance() is False
    assert _gauge() == 0.0


async def test_empty_self_node_is_always_active_without_apisix_call(monkeypatch):
    monkeypatch.setattr(settings, "RUN_BACKGROUND_TASKS", True)
    monkeypatch.setattr(settings, "UNIBRIDGE_SELF_NODE", "")

    from app.services import apisix_client

    async def _fail(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("APISIX must not be consulted in single-instance mode")

    monkeypatch.setattr(apisix_client, "get_resource", _fail)
    assert await is_active_instance() is True
    assert _gauge() == 1.0


async def test_active_when_upstream_points_at_self(monkeypatch):
    monkeypatch.setattr(settings, "RUN_BACKGROUND_TASKS", True)
    monkeypatch.setattr(settings, "UNIBRIDGE_SELF_NODE", "unibridge-service-blue:8000")

    from app.services import apisix_client

    async def _get(kind, rid):
        assert (kind, rid) == ("upstreams", "unibridge-service")
        return {"nodes": {"unibridge-service-blue:8000": 1}}

    monkeypatch.setattr(apisix_client, "get_resource", _get)
    assert await is_active_instance() is True
    assert _gauge() == 1.0


async def test_standby_when_upstream_points_elsewhere(monkeypatch):
    monkeypatch.setattr(settings, "RUN_BACKGROUND_TASKS", True)
    monkeypatch.setattr(settings, "UNIBRIDGE_SELF_NODE", "unibridge-service-blue:8000")

    from app.services import apisix_client

    async def _get(kind, rid):
        return {"nodes": {"unibridge-service-green:8000": 1}}

    monkeypatch.setattr(apisix_client, "get_resource", _get)
    assert await is_active_instance() is False
    assert _gauge() == 0.0


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
    assert _gauge() == 0.0


async def test_fails_open_when_apisix_unreachable(monkeypatch):
    monkeypatch.setattr(settings, "RUN_BACKGROUND_TASKS", True)
    monkeypatch.setattr(settings, "UNIBRIDGE_SELF_NODE", "unibridge-service-blue:8000")

    from app.services import apisix_client

    async def _boom(*args, **kwargs):
        raise ConnectionError("apisix down")

    monkeypatch.setattr(apisix_client, "get_resource", _boom)
    assert await is_active_instance() is True
    # Fail-open assumes active, and the gauge must say so: a standby-looking 0
    # here would let the "no active instance anywhere" rule fire during an
    # APISIX outage, when in fact both colors are running their cycles.
    assert _gauge() == 1.0


async def test_gauge_follows_a_demotion(monkeypatch):
    """The gauge tracks the latest decision, not the first one of the process."""
    monkeypatch.setattr(settings, "RUN_BACKGROUND_TASKS", True)
    monkeypatch.setattr(settings, "UNIBRIDGE_SELF_NODE", "unibridge-service-blue:8000")

    from app.services import apisix_client

    active_node = "unibridge-service-blue:8000"

    async def _get(kind, rid):
        return {"nodes": {active_node: 1}}

    monkeypatch.setattr(apisix_client, "get_resource", _get)
    assert await is_active_instance() is True
    assert _gauge() == 1.0

    active_node = "unibridge-service-green:8000"
    assert await is_active_instance() is False
    assert _gauge() == 0.0


def test_node_normalization_comes_from_the_shared_helper():
    """active_color no longer keeps its own copy of the normalizer."""
    assert not hasattr(active_color, "_node_addresses")
    assert upstream_node_addresses({"a:1": 1, "b:2": 0}) == {"a:1"}
    assert upstream_node_addresses(
        [
            {"host": "h", "port": 9, "weight": 2},
            {"host": "nop", "port": 9, "weight": 0},
            {"host": "noport", "weight": 1},
            "garbage",
        ]
    ) == {"h:9", "noport"}
    assert upstream_node_addresses(None) == set()
    assert upstream_node_addresses("weird") == set()
