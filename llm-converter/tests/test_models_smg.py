"""``GET /v1/models`` filled in with context windows from SMG's ``GET /workers``.

Bifrost lists a model served through Shepherd Model Gateway by id alone, while
SMG reports each model's ``context_length`` per worker. With
``CONVERTER_SMG_PROVIDERS`` set the converter reads from Bifrost's management
API where each provider is registered, asks those URLs beside a listing
refresh, and fills in what the gateway left out — and whatever Bifrost or an
SMG does, the listing still answers.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as converter_main
from app import config
from app.smg_workers import MalformedWorkers, context_lengths

ADMIN_PASSWORD = "bifrost-admin-pass-0000"
SMG_TOKEN = "smg-admin-token-0000"
VIRTUAL_KEY = "sk-bf-gatewaykey00000000000"
ADMIN_BASIC = "Basic " + base64.b64encode(f"admin:{ADMIN_PASSWORD}".encode()).decode()
SECRETS = (ADMIN_PASSWORD, ADMIN_BASIC.split()[1], SMG_TOKEN)


def _json(status: int, body: object) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/json"},
        content=json.dumps(body).encode("utf-8"),
    )


def _listing(*entries: dict | str) -> httpx.Response:
    data = [{"id": entry} if isinstance(entry, str) else entry for entry in entries]
    return _json(200, {"object": "list", "data": data})


def _providers(**base_urls: str) -> httpx.Response:
    """Bifrost's ``GET /api/providers`` as v2.2.6 answers it."""
    return _json(
        200,
        {
            "providers": [
                {
                    "name": name.replace("_", "-"),
                    "network_config": {"base_url": url, "allow_private_network": True},
                    "custom_provider_config": {"base_provider_type": "openai"},
                    "provider_status": "active",
                }
                for name, url in base_urls.items()
            ],
            "total": len(base_urls),
        },
    )


def _worker(*models: dict, **fields: object) -> dict:
    """One ``GET /workers`` record as SMG serializes it, internals included."""
    return {
        "id": "0b7c6a52-0000-0000-0000-000000000000",
        "model_id": models[0]["id"] if models else None,
        "url": "http://gpu-node-17.internal:30000",
        "labels": {"rack": "b12"},
        "models": list(models),
        "worker_type": "regular",
        "is_healthy": True,
        "status": "ready",
        "load": 0,
        **fields,
    }


def _model(model_id: str, context_length: object = None, **fields: object) -> dict:
    card: dict = {"id": model_id, "tokenizer_path": f"/models/checkpoints/{model_id}", **fields}
    if context_length is not None:
        card["context_length"] = context_length
    return card


def _workers(*workers: object) -> httpx.Response:
    return _json(200, {"workers": list(workers), "total": len(workers), "stats": {}})


NOT_FOUND = _json(404, {"detail": "Not Found"})  # what a vLLM answers on /workers


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


class Network:
    """Bifrost and the provider backends behind one mock transport.

    Every route (``"host/path"``) answers from its own list of steps (the last
    repeats) and records the requests it got; an unplanned route is a 404.
    """

    def __init__(self) -> None:
        self.plans: dict[str, list] = {}
        self.calls: dict[str, list[httpx.Request]] = {}

    def serve(self, route: str, *steps: object) -> None:
        self.plans[route] = list(steps)

    def sent(self, route: str) -> list[httpx.Request]:
        return self.calls.get(route, [])

    def asked(self) -> list[str]:
        """Every route that got a request, apart from the Bifrost listing."""
        return sorted(route for route in self.calls if route != "bifrost.test/v1/models")

    def handle(self, request: httpx.Request) -> httpx.Response:
        route = f"{request.url.host}{request.url.path}"
        self.calls.setdefault(route, []).append(request)
        plan = self.plans.get(route)
        if not plan:
            return NOT_FOUND
        step = plan[min(len(self.calls[route]), len(plan)) - 1]
        return step(request) if callable(step) else step


@pytest.fixture
def network(monkeypatch):
    net = Network()
    transport = httpx.MockTransport(net.handle)
    monkeypatch.setattr(
        converter_main,
        "_make_client",
        lambda timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    return net


@pytest.fixture
def clock(monkeypatch):
    now = [10_000.0]
    monkeypatch.setattr(converter_main, "_clock", lambda: now[0])
    return now


@pytest.fixture(autouse=True)
def bifrost(monkeypatch):
    monkeypatch.setenv("CONVERTER_BIFROST_URL", "http://bifrost.test")
    monkeypatch.setenv("BIFROST_ADMIN_PASSWORD", ADMIN_PASSWORD)


@pytest.fixture
def every_provider(monkeypatch):
    monkeypatch.setenv("CONVERTER_SMG_PROVIDERS", "*")


def _one_smg(network: Network, listing: httpx.Response, workers: object) -> None:
    """Bifrost with one provider, ``smg``, registered at ``http://smg.test``."""
    network.serve("bifrost.test/v1/models", listing)
    network.serve("bifrost.test/api/providers", _providers(smg="http://smg.test"))
    network.serve("smg.test/workers", *(workers if isinstance(workers, list) else [workers]))


def _get(headers: dict | None = None) -> httpx.Response:
    return TestClient(converter_main.app).get("/v1/models", headers=headers)


def _entries(resp: httpx.Response) -> dict[str, dict]:
    return {entry["id"]: entry for entry in resp.json()["data"]}


# --- Off unless switched on -------------------------------------------------------------
def test_unset_asks_neither_bifrosts_providers_nor_any_worker(network, monkeypatch):
    monkeypatch.setenv("CONVERTER_SMG_API_KEY", SMG_TOKEN)  # a token alone switches nothing on
    _one_smg(network, _listing("smg/a"), _workers(_worker(_model("a", 131072))))

    resp = _get()

    assert resp.status_code == 200
    assert network.asked() == []
    assert "context_length" not in _entries(resp)["a"]
    assert "authorization" not in network.sent("bifrost.test/v1/models")[0].headers


def test_without_the_bifrost_admin_login_it_stays_off(network, every_provider, monkeypatch):
    monkeypatch.delenv("BIFROST_ADMIN_PASSWORD")
    _one_smg(network, _listing("smg/a"), _workers(_worker(_model("a", 131072))))

    assert "context_length" not in _entries(_get())["a"]
    assert network.asked() == []


# --- The provider URLs come from Bifrost ----------------------------------------------------
def test_every_registered_provider_is_asked_where_bifrost_has_it(network, every_provider):
    network.serve("bifrost.test/v1/models", _listing("smg-a/a", "smg-b/b", "vllm/c"))
    network.serve(
        "bifrost.test/api/providers",
        _providers(smg_a="http://smg-a.test:30000", smg_b="http://smg-b.test/", vllm="http://vllm.test"),
    )
    network.serve("smg-a.test/workers", _workers(_worker(_model("a", 131072))))
    network.serve("smg-b.test/workers", _workers(_worker(_model("b", 65536))))

    entries = _entries(_get())

    assert network.asked() == [
        "bifrost.test/api/providers",
        "smg-a.test/workers",
        "smg-b.test/workers",
        "vllm.test/workers",
    ]
    assert str(network.sent("smg-a.test/workers")[0].url) == "http://smg-a.test:30000/workers"
    assert network.sent("smg-a.test/workers")[0].method == "GET"
    assert entries["a"]["context_length"] == 131072
    assert entries["b"]["context_length"] == 65536
    assert "context_length" not in entries["c"]
    assert list(entries) == ["a", "claude/a", "b", "claude/b", "c", "claude/c"]


def test_a_provider_that_is_no_smg_is_skipped_without_a_word(network, every_provider, caplog):
    network.serve("bifrost.test/v1/models", _listing("vllm/c"))
    network.serve("bifrost.test/api/providers", _providers(vllm="http://vllm.test"))

    with caplog.at_level(logging.WARNING, logger="app.main"):
        resp = _get()

    assert resp.status_code == 200
    assert caplog.records == []


def test_a_provider_moved_in_bifrost_is_followed_there(network, every_provider, clock):
    network.serve("bifrost.test/v1/models", _listing("smg/a"))
    network.serve(
        "bifrost.test/api/providers",
        _providers(smg="http://old.test"),
        _providers(smg="http://new.test"),
    )
    network.serve("old.test/workers", _workers(_worker(_model("a", 4096))))
    network.serve("new.test/workers", _workers(_worker(_model("a", 8192))))
    assert _entries(_get())["a"]["context_length"] == 4096

    clock[0] += 61
    assert _entries(_get())["a"]["context_length"] == 8192
    assert len(network.sent("old.test/workers")) == 1


def test_a_moved_provider_that_fails_does_not_keep_the_old_hosts_answer(
    network, every_provider, clock
):
    network.serve("bifrost.test/v1/models", _listing("smg/a"))
    network.serve(
        "bifrost.test/api/providers",
        _providers(smg="http://old.test"),
        _providers(smg="http://new.test"),
    )
    network.serve("old.test/workers", _workers(_worker(_model("a", 4096))))
    network.serve("new.test/workers", _unreachable)
    _get()

    clock[0] += 61
    assert "context_length" not in _entries(_get())["a"]


def test_a_provider_added_in_bifrost_is_picked_up(network, every_provider, clock):
    network.serve("bifrost.test/v1/models", _listing("smg-a/a"), _listing("smg-a/a", "smg-b/b"))
    network.serve(
        "bifrost.test/api/providers",
        _providers(smg_a="http://smg-a.test"),
        _providers(smg_a="http://smg-a.test", smg_b="http://smg-b.test"),
    )
    network.serve("smg-a.test/workers", _workers(_worker(_model("a", 4096))))
    network.serve("smg-b.test/workers", _workers(_worker(_model("b", 8192))))
    _get()

    clock[0] += 61
    assert _entries(_get())["b"]["context_length"] == 8192


def test_a_provider_removed_from_bifrost_is_dropped(network, every_provider, clock):
    network.serve("bifrost.test/v1/models", _listing("smg/a"))
    network.serve("bifrost.test/api/providers", _providers(smg="http://smg.test"), _providers())
    network.serve("smg.test/workers", _workers(_worker(_model("a", 4096))))
    assert _entries(_get())["a"]["context_length"] == 4096

    clock[0] += 61
    assert "context_length" not in _entries(_get())["a"]
    assert len(network.sent("smg.test/workers")) == 1


def test_only_named_providers_are_asked(network, monkeypatch):
    monkeypatch.setenv("CONVERTER_SMG_PROVIDERS", "smg-a, smg-c")
    network.serve("bifrost.test/v1/models", _listing("smg-a/a", "smg-b/b"))
    network.serve(
        "bifrost.test/api/providers",
        _providers(smg_a="http://smg-a.test", smg_b="http://smg-b.test"),
    )
    network.serve("smg-a.test/workers", _workers(_worker(_model("a", 4096))))
    network.serve("smg-b.test/workers", _workers(_worker(_model("b", 8192))))

    entries = _entries(_get())

    assert network.asked() == ["bifrost.test/api/providers", "smg-a.test/workers"]
    assert entries["a"]["context_length"] == 4096
    assert "context_length" not in entries["b"]


@pytest.mark.parametrize(
    "registered",
    [
        {"providers": [{"name": "smg"}]},  # a built-in provider has no base URL
        {"providers": [{"name": "smg", "network_config": {"base_url": ""}}]},
        {"providers": [{"name": "smg", "network_config": {"base_url": "smg.test:30000"}}]},
        {"providers": [{"name": 7, "network_config": {"base_url": "http://smg.test"}}, None]},
    ],
    ids=["no-network-config", "empty", "no-scheme", "not-a-name"],
)
def test_a_provider_without_a_usable_base_url_is_not_asked(network, every_provider, registered):
    network.serve("bifrost.test/v1/models", _listing("smg/a"))
    network.serve("bifrost.test/api/providers", _json(200, registered))
    network.serve("smg.test/workers", _workers(_worker(_model("a", 4096))))

    assert _get().status_code == 200
    assert network.asked() == ["bifrost.test/api/providers"]


# --- Filling in ---------------------------------------------------------------------------
def test_an_alias_entry_carries_its_models_value(network, every_provider):
    _one_smg(network, _listing("smg/a"), _workers(_worker(_model("a", 65536))))

    entries = _entries(_get())

    assert entries["claude/a"]["context_length"] == entries["a"]["context_length"] == 65536


def test_the_gateways_own_context_length_wins(network, every_provider):
    _one_smg(
        network,
        _listing({"id": "smg/a", "context_length": 32768}, {"id": "smg/b", "context_length": None}),
        _workers(_worker(_model("a", 131072), _model("b", 8192))),
    )

    entries = _entries(_get())

    assert entries["a"]["context_length"] == 32768
    assert entries["claude/a"]["context_length"] == 32768
    assert entries["b"]["context_length"] == 8192  # null is not a value


def test_a_model_is_looked_up_under_the_id_bifrost_sends_its_provider(network, every_provider):
    """A provider key's ``aliases`` map the public name to the served one, and
    Bifrost's listing says so in ``alias``: that is the id the SMG knows."""
    _one_smg(
        network,
        _listing({"id": "smg/qwen3-8b", "alias": "Qwen/Qwen3-8B"}),
        _workers(_worker(_model("Qwen/Qwen3-8B", 131072), _model("qwen3-8b", 4096))),
    )

    entries = _entries(_get())

    assert entries["qwen3-8b"]["context_length"] == 131072
    assert entries["claude/qwen3-8b"]["context_length"] == 131072


@pytest.mark.parametrize(
    "worker_model_id",
    [
        "qwen/qwen3-8b",  # another case
        "Qwen3-8B",  # the name without its organisation
        "smg/Qwen/Qwen3-8B",  # the gateway's own provider-prefixed id
        "/models/checkpoints/Qwen/Qwen3-8B",
        "Qwen/Qwen3-8B ",
    ],
)
def test_only_the_exact_id_matches(network, every_provider, worker_model_id):
    # The right id is in the card too, as an SMG alias and a path: neither is the id.
    card = _model(worker_model_id, 131072, aliases=["Qwen/Qwen3-8B"])
    _one_smg(network, _listing("smg/Qwen/Qwen3-8B"), _workers(_worker(card)))

    assert "context_length" not in _entries(_get())["Qwen/Qwen3-8B"]


def test_a_model_is_not_filled_in_from_another_providers_workers(network, every_provider):
    network.serve("bifrost.test/v1/models", _listing("vllm/a"))
    network.serve(
        "bifrost.test/api/providers", _providers(vllm="http://vllm.test", smg="http://smg.test")
    )
    network.serve("smg.test/workers", _workers(_worker(_model("a", 131072))))

    assert "context_length" not in _entries(_get())["a"]


@pytest.mark.parametrize(
    "fields",
    [
        {"is_healthy": False},
        {"is_healthy": "true"},
        {"status": "draining"},
        {"status": "not_ready"},
        {"stalled": "wedged"},
    ],
    ids=["unhealthy", "healthy-not-a-bool", "draining", "not-ready", "stalled"],
)
def test_a_worker_that_takes_no_traffic_is_not_a_source(network, every_provider, fields):
    _one_smg(network, _listing("smg/a"), _workers(_worker(_model("a", 131072), **fields)))

    assert "context_length" not in _entries(_get())["a"]


def test_a_worker_without_a_status_field_counts_on_is_healthy_alone(network, every_provider):
    worker = _worker(_model("a", 131072))
    del worker["status"]  # older SMG
    _one_smg(network, _listing("smg/a"), _workers(worker))

    assert _entries(_get())["a"]["context_length"] == 131072


# --- Sources that disagree ---------------------------------------------------------------------
def test_workers_that_disagree_leave_the_field_out_and_say_so(network, every_provider, caplog):
    _one_smg(
        network,
        _listing("smg/a", "smg/b"),
        _workers(
            _worker(_model("a", 131072), _model("b", 8192)),
            _worker(_model("a", 32768), _model("b", 8192)),
        ),
    )

    with caplog.at_level(logging.WARNING, logger="app.main"):
        entries = _entries(_get())

    assert "context_length" not in entries["a"]
    assert "context_length" not in entries["claude/a"]
    assert entries["b"]["context_length"] == 8192
    warnings = [r.getMessage() for r in caplog.records if "disagree" in r.getMessage()]
    assert len(warnings) == 1 and "'a'" in warnings[0] and "'smg'" in warnings[0]
    for internal in ("gpu-node-17", "checkpoints", "smg.test", *SECRETS):
        assert internal not in caplog.text, internal


def test_a_standing_disagreement_is_logged_once(network, every_provider, clock, caplog):
    _one_smg(
        network,
        _listing("smg/a"),
        _workers(_worker(_model("a", 131072)), _worker(_model("a", 32768))),
    )

    with caplog.at_level(logging.WARNING, logger="app.main"):
        _get()
        clock[0] += 61
        _get()

    assert len(network.sent("smg.test/workers")) == 2
    assert len([r for r in caplog.records if "disagree" in r.getMessage()]) == 1


def test_an_unhealthy_workers_other_value_is_no_disagreement(network, every_provider):
    _one_smg(
        network,
        _listing("smg/a"),
        _workers(_worker(_model("a", 131072)), _worker(_model("a", 32768), is_healthy=False)),
    )

    assert _entries(_get())["a"]["context_length"] == 131072


def test_a_worker_reporting_no_limit_does_not_hide_anothers(network, every_provider):
    _one_smg(
        network, _listing("smg/a"), _workers(_worker(_model("a")), _worker(_model("a", 131072)))
    )

    assert _entries(_get())["a"]["context_length"] == 131072


@pytest.mark.parametrize(
    ("second", "expected"),
    [(32768, None), (131072, 131072), (None, 131072)],
    ids=["differ", "agree", "second-silent"],
)
def test_one_public_name_on_two_providers(network, every_provider, second, expected):
    """Bifrost sends the name to either provider, so only a value both accept is safe."""
    network.serve("bifrost.test/v1/models", _listing("smg-a/a", "smg-b/a"))
    network.serve(
        "bifrost.test/api/providers",
        _providers(smg_a="http://smg-a.test", smg_b="http://smg-b.test"),
    )
    network.serve("smg-a.test/workers", _workers(_worker(_model("a", 131072))))
    network.serve("smg-b.test/workers", _workers(_worker(_model("a", second))))

    entries = _entries(_get())

    assert list(entries) == ["a", "claude/a"]
    assert entries["a"].get("context_length") == expected


# --- Remote input is untrusted ------------------------------------------------------------
def test_malformed_entries_are_skipped_not_fatal():
    payload = {
        "workers": [
            None,
            "a worker",
            {"is_healthy": True},
            {"is_healthy": True, "models": "a"},
            {"is_healthy": True, "models": [None, "a", {}, {"id": 7, "context_length": 1}]},
            {"is_healthy": True, "models": [{"id": "", "context_length": 4096}]},
            {"is_healthy": True, "models": [{"id": "zero", "context_length": 0}]},
            {"is_healthy": True, "models": [{"id": "negative", "context_length": -1}]},
            {"is_healthy": True, "models": [{"id": "string", "context_length": "131072"}]},
            {"is_healthy": True, "models": [{"id": "float", "context_length": 131072.0}]},
            {"is_healthy": True, "models": [{"id": "bool", "context_length": True}]},
            {"is_healthy": True, "models": [{"id": "good", "context_length": 4096}]},
        ]
    }

    assert context_lengths(payload) == ({"good": 4096}, [])


@pytest.mark.parametrize("payload", [None, [], "workers", {"workers": {}}, {"data": []}])
def test_a_body_without_a_workers_list_is_refused(payload):
    with pytest.raises(MalformedWorkers):
        context_lengths(payload)


def test_nothing_else_of_a_worker_or_a_provider_reaches_the_client(network, every_provider):
    _one_smg(network, _listing("smg/a"), _workers(_worker(_model("a", 4096))))

    resp = _get()

    assert set(_entries(resp)["a"]) == {
        "id",
        "object",
        "type",
        "owned_by",
        "display_name",
        "context_length",
    }
    for internal in ("gpu-node-17", "rack", "checkpoints", "0b7c6a52", "smg.test", *SECRETS):
        assert internal not in resp.text, internal


# --- Each credential goes to its own service and nowhere else -----------------------------------
CALLER_HEADERS = {
    "Authorization": "Bearer client-token",
    "x-api-key": "client-api-key",
    "x-bf-vk": VIRTUAL_KEY,
    "x-bf-dim-consumer": "team-a-key",
    "cookie": "session=abc",
}


def _headers(request: httpx.Request) -> dict[str, str]:
    return {name.lower(): value for name, value in request.headers.items()}


def test_the_admin_login_reads_the_provider_list_and_goes_nowhere_else(network, every_provider):
    _one_smg(network, _listing("smg/a"), _workers(_worker(_model("a", 4096))))

    _get(CALLER_HEADERS)

    to_admin_api = _headers(network.sent("bifrost.test/api/providers")[0])
    assert to_admin_api["authorization"] == ADMIN_BASIC
    assert network.sent("bifrost.test/api/providers")[0].method == "GET"
    to_listing = _headers(network.sent("bifrost.test/v1/models")[0])
    assert to_listing["x-bf-vk"] == VIRTUAL_KEY
    assert "authorization" not in to_listing
    to_smg = _headers(network.sent("smg.test/workers")[0])
    assert "authorization" not in to_smg
    for request in (to_admin_api, to_smg):
        for caller_only in ("x-api-key", "x-bf-vk", "x-bf-dim-consumer", "cookie"):
            assert caller_only not in request, caller_only
        assert "client-token" not in str(request)


def test_another_admin_username_is_used(network, every_provider, monkeypatch):
    monkeypatch.setenv("BIFROST_ADMIN_USERNAME", "ops")
    _one_smg(network, _listing("smg/a"), _workers(_worker(_model("a", 4096))))

    _get()

    expected = "Basic " + base64.b64encode(f"ops:{ADMIN_PASSWORD}".encode()).decode()
    assert network.sent("bifrost.test/api/providers")[0].headers["authorization"] == expected


def test_the_smg_token_goes_to_named_providers_only(network, monkeypatch):
    monkeypatch.setenv("CONVERTER_SMG_PROVIDERS", "smg")
    monkeypatch.setenv("CONVERTER_SMG_API_KEY", SMG_TOKEN)
    _one_smg(network, _listing("smg/a"), _workers(_worker(_model("a", 4096))))

    resp = _get(CALLER_HEADERS)

    assert _headers(network.sent("smg.test/workers")[0])["authorization"] == f"Bearer {SMG_TOKEN}"
    assert _entries(resp)["a"]["context_length"] == 4096
    for route in ("bifrost.test/v1/models", "bifrost.test/api/providers"):
        assert SMG_TOKEN not in str(_headers(network.sent(route)[0])), route


def test_the_smg_token_is_not_sent_to_every_registered_provider(
    network, every_provider, monkeypatch
):
    monkeypatch.setenv("CONVERTER_SMG_API_KEY", SMG_TOKEN)
    network.serve("bifrost.test/v1/models", _listing("smg/a"))
    network.serve(
        "bifrost.test/api/providers",
        _providers(smg="http://smg.test", elsewhere="https://api.elsewhere.test"),
    )
    network.serve("smg.test/workers", _workers(_worker(_model("a", 4096))))

    _get()

    for route in ("smg.test/workers", "api.elsewhere.test/workers"):
        assert "authorization" not in _headers(network.sent(route)[0]), route


def test_a_redirect_is_not_followed(network, every_provider):
    _one_smg(
        network,
        _listing("smg/a"),
        httpx.Response(307, headers={"location": "http://elsewhere.test/workers"}),
    )

    assert _get().status_code == 200
    assert network.sent("elsewhere.test/workers") == []


# --- Nothing here can cost the listing ----------------------------------------------------------
def _oversized(request: httpx.Request) -> httpx.Response:
    # A well-formed answer for the model, so only its size keeps it out.
    body = {
        "workers": [_worker(_model("a", 4096))],
        "padding": "x" * converter_main._WORKERS_BODY_MAX,
    }
    return _json(200, body)


@pytest.mark.parametrize(
    "failure",
    [
        _json(401, {"error": f"invalid token {SMG_TOKEN}"}),
        _json(500, {"error": "boom"}),
        _unreachable,
        httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>login</html>"),
        _json(200, {"data": []}),
        _json(200, [1, 2]),
        _oversized,
    ],
    ids=["401", "500", "unreachable", "html", "no-workers", "a-list", "oversized"],
)
def test_a_failing_provider_leaves_the_listing_as_the_gateway_sent_it(
    network, monkeypatch, failure, caplog
):
    monkeypatch.setenv("CONVERTER_SMG_PROVIDERS", "smg")
    monkeypatch.setenv("CONVERTER_SMG_API_KEY", SMG_TOKEN)
    _one_smg(network, _listing("smg/a", {"id": "smg/b", "context_length": 2048}), failure)

    with caplog.at_level(logging.WARNING, logger="app.main"):
        resp = _get()

    assert resp.status_code == 200
    entries = _entries(resp)
    assert list(entries) == ["a", "claude/a", "b", "claude/b"]
    assert "context_length" not in entries["a"]
    assert entries["b"]["context_length"] == 2048
    assert "no worker metadata from provider 'smg'" in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text and secret not in resp.text


@pytest.mark.parametrize(
    "failure",
    [
        _json(401, {"error": {"message": "Unauthorized"}}),
        _json(500, {"error": "boom"}),
        _unreachable,
        httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>ui</html>"),
        _json(200, {"total": 0}),
    ],
    ids=["401", "500", "unreachable", "html", "no-providers"],
)
def test_a_provider_list_bifrost_will_not_give_leaves_the_listing_alone(
    network, every_provider, failure, caplog
):
    network.serve("bifrost.test/v1/models", _listing("smg/a"))
    network.serve("bifrost.test/api/providers", failure)
    network.serve("smg.test/workers", _workers(_worker(_model("a", 4096))))

    with caplog.at_level(logging.WARNING, logger="app.main"):
        resp = _get()

    assert resp.status_code == 200
    assert "context_length" not in _entries(resp)["a"]
    assert network.sent("smg.test/workers") == []
    assert "Bifrost's provider list is unavailable" in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text and secret not in resp.text


def test_a_standing_failure_is_logged_once(network, every_provider, clock, caplog):
    network.serve("bifrost.test/v1/models", _listing("smg/a"))
    network.serve("bifrost.test/api/providers", _json(401, {"error": {"message": "Unauthorized"}}))

    with caplog.at_level(logging.WARNING, logger="app.main"):
        for _ in range(3):
            _get()
            clock[0] += 31

    assert len(network.sent("bifrost.test/api/providers")) == 3
    assert len(caplog.records) == 1


def test_a_named_provider_bifrost_does_not_have_is_reported(network, monkeypatch, caplog):
    monkeypatch.setenv("CONVERTER_SMG_PROVIDERS", "smg,typo")
    _one_smg(network, _listing("smg/a"), _workers(_worker(_model("a", 4096))))

    with caplog.at_level(logging.WARNING, logger="app.main"):
        resp = _get()

    assert _entries(resp)["a"]["context_length"] == 4096
    assert "'typo'" in caplog.text and "not registered" in caplog.text


def test_an_smg_that_stops_answering_is_reported_even_unnamed(
    network, every_provider, clock, caplog
):
    _one_smg(network, _listing("smg/a"), [_workers(_worker(_model("a", 4096))), _unreachable])

    with caplog.at_level(logging.WARNING, logger="app.main"):
        _get()
        assert caplog.records == []
        clock[0] += 61
        _get()
        clock[0] += 61
        _get()

    assert [r.getMessage() for r in caplog.records] == [
        "converter models: no worker metadata from provider 'smg' (ConnectError)"
    ]


def test_a_gateway_error_is_still_the_gateways_error(network, every_provider):
    _one_smg(
        network,
        _json(401, {"error": {"message": "bad key"}}),
        _workers(_worker(_model("a", 4096))),
    )

    resp = _get()

    assert resp.status_code == 401
    assert resp.json() == {"error": {"message": "bad key"}}


@pytest.mark.parametrize("slow", ["bifrost.test/api/providers", "smg.test/workers"])
async def test_a_slow_round_is_cut_off_and_the_listing_answers(monkeypatch, every_provider, slow):
    monkeypatch.setenv("CONVERTER_SMG_TIMEOUT", "0.05")

    async def handler(request: httpx.Request) -> httpx.Response:
        route = f"{request.url.host}{request.url.path}"
        if route == slow:
            await asyncio.sleep(5)
        if route == "bifrost.test/api/providers":
            return _providers(smg="http://smg.test")
        if route == "smg.test/workers":
            return _workers(_worker(_model("a", 4096)))
        return _listing("smg/a")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        converter_main,
        "_make_client",
        lambda timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
    )
    started = time.monotonic()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=converter_main.app), base_url="http://converter"
    ) as client:
        resp = await client.get("/v1/models")

    assert resp.status_code == 200
    assert time.monotonic() - started < 1.5
    assert "context_length" not in _entries(resp)["a"]


# --- Cache ------------------------------------------------------------------------------------
def test_providers_are_asked_once_per_metadata_ttl_not_per_listing_refresh(
    network, every_provider, clock
):
    _one_smg(
        network,
        _listing("smg/a"),
        [_workers(_worker(_model("a", 4096))), _workers(_worker(_model("a", 8192)))],
    )

    def counts() -> tuple[int, int, int]:
        return tuple(
            len(network.sent(route))
            for route in ("bifrost.test/v1/models", "bifrost.test/api/providers", "smg.test/workers")
        )

    assert _entries(_get())["a"]["context_length"] == 4096
    _get()  # cached listing: nothing is asked
    assert counts() == (1, 1, 1)

    clock[0] += 31  # the listing is refreshed, the metadata (60s) is not due
    assert _entries(_get())["a"]["context_length"] == 4096
    assert counts() == (2, 1, 1)

    clock[0] += 31  # 62s: both
    assert _entries(_get())["a"]["context_length"] == 8192
    assert counts() == (3, 2, 2)


def test_a_failing_provider_keeps_its_last_values_for_the_stale_window(
    network, every_provider, clock
):
    _one_smg(
        network, _listing("smg/a"), [_workers(_worker(_model("a", 4096))), _json(503, {"e": "down"})]
    )
    assert _entries(_get())["a"]["context_length"] == 4096

    clock[0] += 300
    assert _entries(_get())["a"]["context_length"] == 4096
    assert len(network.sent("smg.test/workers")) == 2

    clock[0] += 400  # 700s after the last good answer: past 60 + 600
    assert "context_length" not in _entries(_get())["a"]


def test_values_survive_a_provider_list_that_fails_for_a_while(network, every_provider, clock):
    network.serve("bifrost.test/v1/models", _listing("smg/a"))
    network.serve(
        "bifrost.test/api/providers", _providers(smg="http://smg.test"), _json(500, {"e": "boom"})
    )
    network.serve("smg.test/workers", _workers(_worker(_model("a", 4096))))
    _get()

    clock[0] += 300
    assert _entries(_get())["a"]["context_length"] == 4096
    assert len(network.sent("smg.test/workers")) == 1


def test_a_failed_provider_list_is_not_retried_on_every_call(
    network, every_provider, clock, monkeypatch
):
    monkeypatch.setenv("CONVERTER_MODELS_CACHE_TTL", "0")
    network.serve("bifrost.test/v1/models", _listing("smg/a"))
    network.serve("bifrost.test/api/providers", _unreachable, _providers(smg="http://smg.test"))
    network.serve("smg.test/workers", _workers(_worker(_model("a", 4096))))

    _get()
    _get()
    assert len(network.sent("bifrost.test/v1/models")) == 2
    assert len(network.sent("bifrost.test/api/providers")) == 1

    clock[0] += 11
    assert _entries(_get())["a"]["context_length"] == 4096


def test_a_stale_listing_is_still_filled_in(network, every_provider, clock):
    _one_smg(network, _listing("smg/a"), _workers(_worker(_model("a", 4096))))
    network.serve(
        "bifrost.test/v1/models", _listing("smg/a"), _json(503, {"error": {"message": "down"}})
    )
    _get()

    clock[0] += 300
    resp = _get()

    assert resp.status_code == 200
    assert _entries(resp)["a"]["context_length"] == 4096


def test_another_selection_does_not_use_the_old_ones_answers(
    network, every_provider, clock, monkeypatch
):
    _one_smg(network, _listing("smg/a"), _workers(_worker(_model("a", 4096))))
    assert _entries(_get())["a"]["context_length"] == 4096

    monkeypatch.setenv("CONVERTER_SMG_PROVIDERS", "another")
    assert "context_length" not in _entries(_get())["a"]  # even from the cached listing


# --- Settings -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "models_timeout", "expected"),
    [
        (None, None, 1.5),
        ("0", None, 1.5),  # no unbounded setting
        ("abc", None, 1.5),
        ("0.5", None, 0.5),
        ("10", None, 1.5),  # held to 3/4 of the caller's 2s wait
        ("10", "8", 6.0),
        ("10", "0", 10.0),  # callers wait up to the fetch ceiling
    ],
)
def test_smg_timeout_stays_inside_the_callers_wait(monkeypatch, raw, models_timeout, expected):
    if raw is not None:
        monkeypatch.setenv("CONVERTER_SMG_TIMEOUT", raw)
    if models_timeout is not None:
        monkeypatch.setenv("CONVERTER_MODELS_TIMEOUT", models_timeout)

    assert config.settings.smg_timeout == pytest.approx(expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        (" , ,", None),
        ("*", "*"),
        (" smg-a , smg-b,", frozenset({"smg-a", "smg-b"})),
        ("smg-a,*", "*"),
    ],
)
def test_smg_providers_env_modes(monkeypatch, raw, expected):
    if raw is not None:
        monkeypatch.setenv("CONVERTER_SMG_PROVIDERS", raw)

    assert config.settings.smg_providers == expected
