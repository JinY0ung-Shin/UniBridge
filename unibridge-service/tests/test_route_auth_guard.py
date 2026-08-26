"""Guard test: no route reaches a handler without passing an auth gate.

Every router is mounted by hand in ``app.main`` and every endpoint declares its
own gate, so a new route that simply forgets ``Depends(require_permission(...))``
is a silent, fully-public endpoint — nothing in the suite noticed before this
file. These tests walk the *live* dependency graph of every mounted route, so a
router added later is covered without anyone touching this file.

"Guarded" means: somewhere in the route's flattened dependency graph there is a
callable defined in :mod:`app.auth`. That module exposes exactly three
dependencies (``KNOWN_AUTH_GATES``) and each raises 401/403 on its own, so the
module name is a sufficient test — ``test_auth_dependencies_are_all_known_gates``
holds that invariant in place. The ``HTTPBearer`` instance in that module does
*not* satisfy the rule, because an instance reports its class's
``fastapi.security.http`` module. That is the right outcome: it is built with
``auto_error=False`` and rejects nothing by itself.

Scope is ``fastapi.routing.APIRoute``. FastAPI's own ``/openapi.json``, ``/docs``
and ``/redoc`` are plain Starlette ``Route`` objects with no dependency graph to
inspect, so they are out of reach here rather than allowlisted below.
"""
from __future__ import annotations

from typing import Iterator

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.main import app


AUTH_MODULE = "app.auth"

# Every callable app.auth hands to ``Depends()``. The module-name rule above is
# what decides "guarded"; this set exists so that a *new* app.auth dependency
# has to be consciously classified as a gate instead of silently satisfying the
# rule by virtue of where it lives.
KNOWN_AUTH_GATES = {
    "get_current_user",                     # Bearer JWT (Keycloak RS256 / dev HS256)
    "get_current_user_or_apikey",           # APISIX consumer header OR Bearer JWT
    "require_permission.<locals>.checker",  # RBAC; depends on get_current_user
}

# (method, path) pairs that legitimately take no authentication at all. Each
# entry needs a comment saying why, and
# ``test_public_routes_allowlist_has_no_stale_entries`` fails on the ones that
# have stopped being true so they get removed rather than accumulating.
PUBLIC_ROUTES = {
    # Container liveness probe — the compose healthcheck for both colours GETs
    # it before any credential exists (docker-compose.app.yml:109,
    # docker-compose.yml:278). Returns a bare {"status": "ok"}; the variant that
    # actually touches the registered databases (/health/databases) is gated on
    # query.databases.read.
    ("GET", "/health"),
    # Prometheus scrape target, mounted by prometheus_fastapi_instrumentator
    # (app/main.py:770) rather than by a router. Reachable in-cluster only: the
    # UI nginx returns 404 for /_api/metrics and /_api/metrics/
    # (unibridge-ui/nginx.conf:71-76), which is the whole of its access control.
    ("GET", "/metrics"),
    # Dev/testing token mint (app/main.py:829-858). Issuing a token is exactly
    # what cannot require a token; safe only because the route is not mounted at
    # all unless ENABLE_DEV_TOKEN_ENDPOINT is set, which defaults to false in
    # config.py and in every compose file. tests/conftest.py sets it to "true",
    # so the route always exists under pytest.
    ("POST", "/auth/token"),
}

# Routes that authenticate inside the handler body instead of through a
# dependency, mapped to the verifier they must keep calling. Dependency-graph
# inspection cannot see these, so they are allowlisted by name — but
# ``test_handler_auth_routes_still_verify_inside_the_handler`` re-checks that the
# call is still there, which a bare allowlist entry would not.
HANDLER_AUTH_ROUTES = {
    # Alertmanager webhook: a shared bearer token (ALERTMANAGER_WEBHOOK_TOKEN),
    # never a user JWT, and 503 when the setting is empty. Deliberately not a
    # Depends() — the sender is a machine and the endpoint carries no user
    # identity. See app/routers/internal_alerts.py:105.
    ("POST", "/internal/alertmanager"): "_verify_token",
}


# ── Helpers ─────────────────────────────────────────────────────────────────


def _api_routes() -> list[APIRoute]:
    return [route for route in app.routes if isinstance(route, APIRoute)]


def _flatten(dependant: Dependant) -> Iterator[Dependant]:
    """Yield a route's ``Dependant`` plus every transitive sub-dependency.

    FastAPI merges all four injection sites into ``route.dependant`` before the
    route is served — handler parameters, path-decorator ``dependencies=[...]``,
    ``APIRouter(dependencies=[...])`` and ``include_router(dependencies=[...])``
    — so walking this one tree sees a gate declared in any of them.
    """
    yield dependant
    for sub in dependant.dependencies:
        yield from _flatten(sub)


def _auth_gates(route: APIRoute) -> set[str]:
    """Names of the app.auth dependencies guarding ``route`` (empty = unguarded)."""
    return {
        getattr(dep.call, "__qualname__", repr(dep.call))
        for dep in _flatten(route.dependant)
        if dep.call is not None
        and getattr(dep.call, "__module__", None) == AUTH_MODULE
    }


def _endpoints(route: APIRoute) -> set[tuple[str, str]]:
    return {(method, route.path) for method in route.methods}


def _where(route: APIRoute) -> str:
    """``module::qualname:line`` of the handler, for a jump-to-it failure message."""
    fn = route.endpoint
    code = getattr(fn, "__code__", None)
    line = f":{code.co_firstlineno}" if code is not None else ""
    return f"{fn.__module__}::{getattr(fn, '__qualname__', fn)}{line}"


def _routes_by_endpoint() -> dict[tuple[str, str], APIRoute]:
    return {ep: route for route in _api_routes() for ep in _endpoints(route)}


# ── The guard ───────────────────────────────────────────────────────────────


def test_every_route_is_behind_an_auth_gate():
    allowlisted = PUBLIC_ROUTES | set(HANDLER_AUTH_ROUTES)
    unguarded = [
        f"{method} {path}  ->  {_where(route)}"
        for route in _api_routes()
        if not _auth_gates(route)
        for method, path in sorted(_endpoints(route))
        if (method, path) not in allowlisted
    ]
    assert not unguarded, (
        f"{len(unguarded)} route(s) reach their handler with no app.auth "
        "dependency, i.e. unauthenticated. Add "
        "Depends(require_permission(...)) or Depends(get_current_user) — or, if "
        "the route is genuinely meant to be public, add it to PUBLIC_ROUTES in "
        "tests/test_route_auth_guard.py with a comment saying why:\n  "
        + "\n  ".join(sorted(unguarded))
    )


def test_dependency_walk_actually_resolves_gates():
    """Guard on the guard: the walk must still see the gates that do exist.

    A bug in ``_flatten``/``_auth_gates`` that returned an empty set for every
    route would make the test above pass with an empty list — vacuously green
    while the app went unchecked. Assert the positive direction too.
    """
    guarded = [route for route in _api_routes() if _auth_gates(route)]
    assert len(guarded) > 100, (
        "Expected most of the ~150 mounted routes to resolve an app.auth "
        f"dependency, found {len(guarded)} of {len(_api_routes())} — the "
        "dependency-graph walk is probably broken, not the app."
    )


# ── Allowlist hygiene ───────────────────────────────────────────────────────


def test_public_routes_allowlist_has_no_stale_entries():
    """A dead PUBLIC_ROUTES entry silently re-permits a route later.

    Both failure modes matter: an entry for a route that no longer exists is
    noise, and an entry for a route that has since *grown* a gate would keep the
    guard quiet if that gate were removed again.
    """
    routes = _routes_by_endpoint()
    stale = []
    for endpoint in sorted(PUBLIC_ROUTES):
        route = routes.get(endpoint)
        if route is None:
            stale.append(f"{endpoint[0]} {endpoint[1]}  — no such route any more")
            continue
        gates = _auth_gates(route)
        if gates:
            stale.append(
                f"{endpoint[0]} {endpoint[1]}  — now guarded by "
                f"{', '.join(sorted(gates))}"
            )
    assert not stale, (
        "Stale PUBLIC_ROUTES entries in tests/test_route_auth_guard.py — remove "
        "them:\n  " + "\n  ".join(stale)
    )


def test_handler_auth_routes_still_verify_inside_the_handler():
    routes = _routes_by_endpoint()
    problems = []
    for endpoint, verifier in sorted(HANDLER_AUTH_ROUTES.items()):
        route = routes.get(endpoint)
        if route is None:
            problems.append(f"{endpoint[0]} {endpoint[1]}  — no such route any more")
            continue
        code = getattr(route.endpoint, "__code__", None)
        if code is None or verifier not in code.co_names:
            problems.append(
                f"{endpoint[0]} {endpoint[1]}  — {_where(route)} no longer calls "
                f"{verifier}(), so the route is now unauthenticated"
            )
    assert not problems, (
        "HANDLER_AUTH_ROUTES no longer describes reality. Either the handler "
        "lost its in-body auth check (fix the handler) or it moved to a "
        "Depends() gate (drop the entry):\n  " + "\n  ".join(problems)
    )


def test_auth_dependencies_are_all_known_gates():
    """Keep the "defined in app.auth" rule honest.

    ``_auth_gates`` trusts the module a dependency lives in. That holds only
    while every app.auth dependency really is a gate, so a new one has to be
    classified here — as a gate, by adding it — rather than counting as auth for
    free.
    """
    seen: set[str] = set()
    for route in _api_routes():
        seen |= _auth_gates(route)
    unknown = sorted(seen - KNOWN_AUTH_GATES)
    assert not unknown, (
        "New app.auth dependency/dependencies in use that this test has never "
        f"classified: {', '.join(unknown)}. If they raise 401/403 on their own, "
        "add them to KNOWN_AUTH_GATES. If they do not, they must not be treated "
        "as auth — narrow _auth_gates() to an explicit set of callables."
    )
