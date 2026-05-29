"""One-off migration: downgrade legacy ``developer``/``viewer`` realm roles to ``user``.

Context
-------
The RBAC model has been simplified from a three-tier hierarchy
(``admin`` > ``developer`` > ``viewer``) to a two-tier one (``admin`` > ``user``).
This script walks every Keycloak user in the configured realm, and for any
user that still holds the obsolete ``developer`` or ``viewer`` realm role:

1. Assigns the new ``user`` realm role (idempotent — Keycloak ignores
   duplicate assignments).
2. Removes ``developer`` and/or ``viewer`` from the user.

Users that already have ``admin`` are left untouched (admin is the higher
tier and is not affected by this consolidation). Users that have neither
obsolete role nor admin are skipped — they may have already been migrated,
or never had an app role.

Following the pattern in ``app.routers.users.change_role``, the new role
is assigned first so the user is never temporarily without a role.

Usage
-----
Run from the ``unibridge-service`` directory with Keycloak environment
variables configured (``KEYCLOAK_URL``, ``KEYCLOAK_REALM``,
``KEYCLOAK_SERVICE_CLIENT_ID``, ``KEYCLOAK_SERVICE_CLIENT_SECRET``)::

    python -m scripts.migrate_roles_to_user

The script is idempotent: re-running after a successful migration finds
0 users to migrate.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.keycloak_admin import KeycloakAdminClient

logger = logging.getLogger(__name__)

OBSOLETE_ROLES = {"developer", "viewer"}
NEW_ROLE = "user"
ADMIN_ROLE = "admin"

# Page size for listing Keycloak users.
PAGE_SIZE = 100


async def migrate(kc: KeycloakAdminClient) -> dict[str, int]:
    """Migrate every user with ``developer``/``viewer`` to ``user``.

    Returns a counters dict: ``{"migrated", "skipped_admin", "skipped_other", "errors"}``.
    """
    # Verify the new role exists before we start assigning it. ``assign_realm_role``
    # also performs this lookup per-call, but failing fast gives a clearer error.
    realm_roles = await kc.get_realm_roles()
    realm_role_names = {r["name"] for r in realm_roles}
    if NEW_ROLE not in realm_role_names:
        raise RuntimeError(
            f"Realm role {NEW_ROLE!r} does not exist in realm "
            f"{kc.realm!r}; create it before running this migration."
        )

    counters = {"migrated": 0, "skipped_admin": 0, "skipped_other": 0, "errors": 0}

    first = 0
    while True:
        users, total = await kc.list_users(first=first, max_results=PAGE_SIZE)
        if not users:
            break

        logger.info(
            "Processing users %d-%d of %d",
            first,
            first + len(users) - 1,
            total,
        )

        for user in users:
            user_id = user["id"]
            username = user.get("username", "<unknown>")
            try:
                user_roles = await kc.get_user_realm_roles(user_id)
                role_names = {r["name"] for r in user_roles}

                if ADMIN_ROLE in role_names:
                    logger.info("skip admin user %s (id=%s)", username, user_id)
                    counters["skipped_admin"] += 1
                    continue

                obsolete_held = role_names & OBSOLETE_ROLES
                if not obsolete_held:
                    counters["skipped_other"] += 1
                    continue

                # Assign new role first so user is never without an app role.
                await kc.assign_realm_role(user_id, NEW_ROLE)
                for old_role in obsolete_held:
                    await kc.remove_realm_role(user_id, old_role)

                logger.info(
                    "migrated %s (id=%s): removed %s, assigned %s",
                    username,
                    user_id,
                    sorted(obsolete_held),
                    NEW_ROLE,
                )
                counters["migrated"] += 1
            except Exception:
                logger.exception(
                    "failed to migrate user %s (id=%s)", username, user_id
                )
                counters["errors"] += 1

        first += len(users)
        if first >= total:
            break

    return counters


async def main() -> None:
    """Async entrypoint: build a KeycloakAdminClient, migrate, then close it."""
    if not settings.KEYCLOAK_URL:
        raise RuntimeError(
            "KEYCLOAK_URL is not configured; cannot run role migration."
        )

    kc = KeycloakAdminClient(
        base_url=settings.KEYCLOAK_URL,
        realm=settings.KEYCLOAK_REALM,
        client_id=settings.KEYCLOAK_SERVICE_CLIENT_ID,
        client_secret=settings.KEYCLOAK_SERVICE_CLIENT_SECRET,
    )
    try:
        counters = await migrate(kc)
    finally:
        await kc.close()

    logger.info(
        "Done. migrated=%d, skipped_admin=%d, skipped_other=%d, errors=%d",
        counters["migrated"],
        counters["skipped_admin"],
        counters["skipped_other"],
        counters["errors"],
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
