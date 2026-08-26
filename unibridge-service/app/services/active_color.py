"""Blue/green active-color detection for in-app background work.

Production runs two app stacks (blue + green) against the same meta DB,
Prometheus, and APISIX; the standby color stays up for instant rollback.
Side-effectful background work (alert checker cycles, retention cleanup,
Prometheus-webhook dispatch) must therefore run on exactly one color, or
operators get duplicate alert mails and the two processes clobber each
other's persisted alert state.

The APISIX ``unibridge-service`` upstream is the runtime source of truth for
which color is active — the deploy script's promote step is its only writer.
Consulting it per cycle (instead of an env baked in at container creation)
stays correct across promotes, rollbacks, and container restarts without ever
recreating containers.
"""
from __future__ import annotations

import logging

from app import metrics
from app.config import settings

logger = logging.getLogger(__name__)

_last_logged_active: bool | None = None


async def is_active_instance() -> bool:
    """Return True when this instance should run side-effectful background work.

    - ``RUN_BACKGROUND_TASKS=false`` is a hard off-switch (never active).
    - Empty ``UNIBRIDGE_SELF_NODE`` means single-instance mode (dev, tests,
      the single-stack compose file): always active.
    - Otherwise active only while the APISIX ``unibridge-service`` upstream
      routes to this node. Fails open when APISIX is unreachable: with no way
      to tell which color is active, a brief window of duplicate alerts beats
      both colors going silent.

    Every decision is published to the ``unibridge_active_instance`` gauge —
    that is the only place the "no color is active anywhere" failure is
    visible, since neither process can observe the other's silence.
    """
    global _last_logged_active

    if not settings.RUN_BACKGROUND_TASKS:
        metrics.set_active_instance(False)
        return False
    self_node = settings.UNIBRIDGE_SELF_NODE.strip()
    if not self_node:
        metrics.set_active_instance(True)
        return True

    try:
        from app.services import apisix_client

        upstream = await apisix_client.get_resource("upstreams", "unibridge-service")
        active = self_node in apisix_client.upstream_node_addresses(upstream.get("nodes"))
    except Exception as exc:
        if _last_logged_active is not True:
            logger.warning(
                "Active-color check against APISIX failed (%s) — assuming active", exc
            )
            _last_logged_active = True
        metrics.set_active_instance(True)
        return True

    if active is not _last_logged_active:
        logger.info(
            "Active-color check: self=%s active=%s — background work %s",
            self_node,
            active,
            "enabled" if active else "paused (standby color)",
        )
        _last_logged_active = active
    metrics.set_active_instance(active)
    return active
