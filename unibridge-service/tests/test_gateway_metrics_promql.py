"""PromQL injection defences on the gateway monitoring endpoints.

Every metrics endpoint builds its Prometheus query by string interpolation, so a
caller-supplied filter that reaches a label matcher unescaped can close the
selector and query series it was never scoped to (``x",job=~".+`` turns
``{consumer="x"}`` into ``{consumer="x",job=~".+"}``). Two independent layers
are pinned here:

1. Every endpoint that takes a ``consumer``/``api_key`` filter rejects a value
   that is not an API-key name, before any Prometheus call. The check is driven
   off the router's own signatures so a new endpoint cannot quietly skip it.
2. The selector builders escape what they interpolate, so a value that somehow
   reaches them still lands inside one label matcher.
"""

from __future__ import annotations

import inspect
import re
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.routers import gateway
from app.routers.gateway import (
    _labels,
    _llm_consumer_extra,
    _llm_key_selector,
    _promql_str,
)
from tests.conftest import auth_header

# Closes the label matcher, then widens the selector to every series.
BREAKOUT = 'x",job=~".+'

# Caller-supplied query params that end up in a PromQL label matcher.
FILTER_PARAMS = ("consumer", "api_key")


def _filtered_endpoints() -> list[tuple[str, str]]:
    """(path, param) for every gateway endpoint taking a PromQL filter param."""
    found: list[tuple[str, str]] = []
    for route in gateway.router.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        params = inspect.signature(endpoint).parameters
        for name in FILTER_PARAMS:
            if name in params:
                found.append((route.path, name))
    return sorted(found)


class TestEveryFilterParamIsValidated:
    def test_the_scan_found_the_endpoints(self):
        """A refactor that renames the params must not silently empty this suite."""
        paths = {path for path, _ in _filtered_endpoints()}
        assert "/admin/gateway/metrics/summary" in paths
        assert "/admin/gateway/metrics/llm/summary" in paths
        assert len(paths) >= 10

    @pytest.mark.parametrize("path,param", _filtered_endpoints())
    async def test_selector_breakout_is_rejected(self, client, admin_token, path, param):
        instant = AsyncMock(return_value=[])
        range_q = AsyncMock(return_value=[])
        with (
            patch("app.routers.gateway.prometheus_client.instant_query", instant),
            patch("app.routers.gateway.prometheus_client.range_query", range_q),
        ):
            resp = await client.get(
                path, params={param: BREAKOUT}, headers=auth_header(admin_token)
            )
        assert resp.status_code == 400, f"{path}?{param}= accepted a hostile value"
        assert resp.json()["detail"] == "Invalid consumer name"
        instant.assert_not_awaited()
        range_q.assert_not_awaited()

    @pytest.mark.parametrize("path,param", _filtered_endpoints())
    async def test_a_real_key_name_still_filters(self, client, admin_token, path, param):
        """The guard must not be over-broad: an ordinary key name still queries."""
        instant = AsyncMock(return_value=[])
        range_q = AsyncMock(return_value=[])
        with (
            patch("app.routers.gateway.prometheus_client.instant_query", instant),
            patch("app.routers.gateway.prometheus_client.range_query", range_q),
            patch(
                "app.routers.gateway.apisix_client.list_resources",
                new_callable=AsyncMock,
                return_value={"items": [], "total": 0},
            ),
        ):
            resp = await client.get(
                path, params={param: "svc.prod-1"}, headers=auth_header(admin_token)
            )
        assert resp.status_code == 200, path
        queries = [
            call.args[0]
            for call in list(instant.call_args_list) + list(range_q.call_args_list)
        ]
        assert queries, f"{path} issued no Prometheus query"
        assert any('"svc.prod-1"' in q for q in queries)


class TestLlmKeyFilterValidation:
    """The LLM endpoints authorize on ``gateway.monitoring.read`` and never went
    through ``_scope_consumer``, so their ``api_key`` filter was unvalidated."""

    @pytest.mark.parametrize(
        "bad",
        [BREAKOUT, 'a"} or on(x) ', "a b", "name/etc", "x\"y", 'end_user!="'],
    )
    def test_selector_builders_reject_hostile_values(self, bad):
        for builder in (_llm_key_selector, _llm_consumer_extra):
            with pytest.raises(HTTPException) as ei:
                builder(bad)
            assert ei.value.status_code == 400

    def test_safe_values_build_the_selector(self):
        assert _llm_key_selector("svc.prod-1") == '{end_user="svc.prod-1"}'
        assert _llm_consumer_extra("svc.prod-1") == ('consumer="svc.prod-1"',)

    def test_unscoped_stays_unscoped(self):
        assert _llm_key_selector(None) == ""
        assert _llm_key_selector("") == ""
        assert _llm_consumer_extra(None) == ()
        assert _llm_consumer_extra("") == ()


class TestLabelsEscapesConsumer:
    """``_labels`` escapes independently of the endpoint-level validation: the
    two layers must not both have to hold for the selector to stay intact."""

    def test_breakout_stays_one_label(self):
        selector = _labels("r1", BREAKOUT)
        assert selector == '{route="r1",consumer="x\\",job=~\\".+"}'
        # Two matchers, so four unescaped quotes: the injected label never
        # becomes syntax, it stays inside the consumer value.
        assert len(re.findall(r'(?<!\\)"', selector)) == 4
        assert ',job=~".+"' not in selector

    def test_backslashes_are_doubled(self):
        assert _labels(None, "a\\b") == (
            '{route!="llm-proxy",route!="llm-messages",'
            'route!="llm-responses",consumer="a\\\\b"}'
        )

    def test_matches_promql_str(self):
        assert f'consumer="{_promql_str(BREAKOUT)}"' in _labels("r1", BREAKOUT)


class TestUsagesConsumerEscaping:
    """``usages_payload`` builds its own selector for the include_llm case."""

    async def test_llm_usages_escapes_the_consumer(self, client, admin_token):
        mock = AsyncMock(return_value=[])
        with (
            patch("app.routers.gateway.prometheus_client.instant_query", mock),
            patch(
                "app.routers.gateway.apisix_client.list_resources",
                new_callable=AsyncMock,
                return_value={"items": [], "total": 0},
            ),
        ):
            resp = await client.get(
                "/admin/gateway/metrics/usages",
                params={"consumer": "svc.prod-1", "include_llm": "true"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 200
        assert mock.call_args.args[0].count("{") == 1
        assert 'consumer="svc.prod-1"' in mock.call_args.args[0]

    async def test_llm_usages_rejects_a_breakout(self, client, admin_token):
        mock = AsyncMock(return_value=[])
        with patch("app.routers.gateway.prometheus_client.instant_query", mock):
            resp = await client.get(
                "/admin/gateway/metrics/usages",
                params={"consumer": BREAKOUT, "include_llm": "true"},
                headers=auth_header(admin_token),
            )
        assert resp.status_code == 400
        mock.assert_not_awaited()
