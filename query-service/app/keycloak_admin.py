"""Keycloak Admin REST API client for user management."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


class KeycloakAdminClient:
    """Async client for the Keycloak Admin REST API using Client Credentials grant."""

    def __init__(
        self,
        base_url: str,
        realm: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.realm = realm
        self.client_id = client_id
        self.client_secret = client_secret

        self._token: str | None = None
        self._token_expires_at: float = 0.0
        from app.config import settings as _settings
        ssl_verify: str | bool = _settings.SSL_CA_CERT_PATH or _settings.SSL_VERIFY
        self._client = httpx.AsyncClient(timeout=10.0, verify=ssl_verify)
        self._token_lock = asyncio.Lock()

    # ── Token management ─────────────────────────────────────────────────

    async def get_token(self) -> str:
        """Obtain a service-account access token via Client Credentials Grant.

        The token is cached until it expires (with a 30-second safety margin).
        """
        now = time.time()
        if self._token and now < self._token_expires_at - 30:
            return self._token

        async with self._token_lock:
            # Double-check after acquiring lock
            now = time.time()
            if self._token and now < self._token_expires_at - 30:
                return self._token

            token_url = (
                f"{self.base_url}/realms/{self.realm}/protocol/openid-connect/token"
            )
            resp = await self._client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )
            if resp.status_code != 200:
                logger.error("Keycloak token request failed: %s %s", resp.status_code, resp.text)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Failed to obtain Keycloak service token",
                )
            data = resp.json()
            self._token = data["access_token"]
            # Cache with 30-second safety margin
            self._token_expires_at = now + data.get("expires_in", 300)
            return self._token

    # ── HTTP helper ──────────────────────────────────────────────────────

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Send an authenticated request to the Keycloak Admin API."""
        token = await self.get_token()
        url = f"{self.base_url}/admin/realms/{self.realm}{path}"
        resp = await self._client.request(
            method.upper(),
            url,
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
        return resp

    # ── User CRUD ────────────────────────────────────────────────────────

    async def get_user(self, user_id: str) -> dict:
        """Get a single user by Keycloak ID."""
        resp = await self._request("GET", f"/users/{user_id}")
        if resp.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        resp.raise_for_status()
        return resp.json()

    async def list_users(
        self,
        search: str | None = None,
        first: int = 0,
        max_results: int = 50,
    ) -> tuple[list[dict], int]:
        """List users with optional search, returning (users, total_count)."""
        params: dict = {"first": first, "max": max_results}
        if search:
            params["search"] = search

        # Get the count first
        count_params = {}
        if search:
            count_params["search"] = search
        count_resp = await self._request("GET", "/users/count", params=count_params)
        if count_resp.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to count Keycloak users",
            )
        total = count_resp.json()

        # Get the page of users
        resp = await self._request("GET", "/users", params=params)
        if resp.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to list Keycloak users",
            )
        return resp.json(), total

    async def create_user(
        self,
        username: str,
        email: str | None = None,
        password: str | None = None,
        enabled: bool = True,
    ) -> str:
        """Create a new Keycloak user. Returns the user ID from the Location header."""
        payload: dict = {
            "username": username,
            "enabled": enabled,
        }
        if email:
            payload["email"] = email
        if password:
            payload["credentials"] = [
                {"type": "password", "value": password, "temporary": False}
            ]

        resp = await self._request("POST", "/users", json=payload)
        if resp.status_code == 409:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"User '{username}' already exists",
            )
        if resp.status_code != 201:
            logger.error("Failed to create user: %s %s", resp.status_code, resp.text)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to create Keycloak user",
            )
        # Extract user ID from Location header
        location = resp.headers.get("location", "")
        user_id = location.rsplit("/", 1)[-1]
        return user_id

    async def update_user_enabled(self, user_id: str, enabled: bool) -> None:
        """Enable or disable a user."""
        resp = await self._request("PUT", f"/users/{user_id}", json={"enabled": enabled})
        if resp.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        if resp.status_code != 204:
            logger.error("Failed to update user enabled status: %s %s", resp.status_code, resp.text)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to update Keycloak user",
            )

    async def delete_user(self, user_id: str) -> None:
        """Delete a user by ID."""
        resp = await self._request("DELETE", f"/users/{user_id}")
        if resp.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        if resp.status_code != 204:
            logger.error("Failed to delete user: %s %s", resp.status_code, resp.text)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to delete Keycloak user",
            )

    async def reset_password(
        self,
        user_id: str,
        password: str,
        temporary: bool = True,
    ) -> None:
        """Reset a user's password."""
        resp = await self._request(
            "PUT",
            f"/users/{user_id}/reset-password",
            json={"type": "password", "value": password, "temporary": temporary},
        )
        if resp.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        if resp.status_code != 204:
            logger.error("Failed to reset password: %s %s", resp.status_code, resp.text)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to reset Keycloak user password",
            )

    # ── Realm Roles ──────────────────────────────────────────────────────

    async def get_realm_roles(self) -> list[dict]:
        """Get all realm-level roles."""
        resp = await self._request("GET", "/roles")
        if resp.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to list Keycloak realm roles",
            )
        return resp.json()

    async def get_user_realm_roles(self, user_id: str) -> list[dict]:
        """Get realm-level roles assigned to a user."""
        resp = await self._request("GET", f"/users/{user_id}/role-mappings/realm")
        if resp.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        if resp.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to get user realm roles",
            )
        return resp.json()

    async def assign_realm_role(self, user_id: str, role_name: str) -> None:
        """Assign a realm role to a user (looks up role representation first)."""
        # Look up the role representation
        roles = await self.get_realm_roles()
        role_rep = next((r for r in roles if r["name"] == role_name), None)
        if role_rep is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Realm role '{role_name}' not found",
            )

        resp = await self._request(
            "POST",
            f"/users/{user_id}/role-mappings/realm",
            json=[{"id": role_rep["id"], "name": role_rep["name"]}],
        )
        if resp.status_code not in (200, 204):
            logger.error("Failed to assign role: %s %s", resp.status_code, resp.text)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to assign realm role",
            )

    async def remove_realm_role(self, user_id: str, role_name: str) -> None:
        """Remove a realm role from a user."""
        # Look up the role representation
        roles = await self.get_realm_roles()
        role_rep = next((r for r in roles if r["name"] == role_name), None)
        if role_rep is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Realm role '{role_name}' not found",
            )

        resp = await self._request(
            "DELETE",
            f"/users/{user_id}/role-mappings/realm",
            json=[{"id": role_rep["id"], "name": role_rep["name"]}],
        )
        if resp.status_code not in (200, 204):
            logger.error("Failed to remove role: %s %s", resp.status_code, resp.text)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to remove realm role",
            )
