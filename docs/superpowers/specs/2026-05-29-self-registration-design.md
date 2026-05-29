# Approval-Gated Self-Registration

**Date:** 2026-05-29
**Status:** Implemented
**Model:** Open registration, but every new account is **pending** (no role) until an admin approves it by assigning a role.

## Problem

A brand-new person had **no way to create an account** — `registrationAllowed: false`, no in-app signup, and the only user-creation endpoint (`POST /admin/users`) is admin-only. Account creation was entirely admin-driven.

## Goal

A new person can self-register, but cannot access the service until an admin approves them.

```
register → pending (no application role) → admin assigns a role → access
```

No custom Keycloak extension (SPI) is used — Keycloak has no native registration-approval feature, so the gate is built from existing pieces.

## Mechanism (why this works without an SPI)

- **Pending = role-less.** Keycloak registration creates an *enabled* user, but if the realm's default roles do **not** include an app role, the new user has no `admin`/`user` role.
- **Backend already blocks role-less users.** `app/auth.py:_verify_keycloak_token` resolves the app role from the token and raises `401 "Token missing username or valid role"` when none is present (`auth.py:229`). So a pending user is fully denied at the API.
- **Approval = assign a role.** The admin UI **Users** page lists all Keycloak users (role-less ones shown as *Pending*) and can assign the `user` role via the existing `change_role` flow.

## Changes

### 1. Keycloak realm (`keycloak/realm-export.json` + helper script)
- `registrationAllowed: true`, `bruteForceProtected: true`.
- `user` is **not** added to the `default-roles-apihub` composite → new users are pending.
- `keycloak/enable-self-registration.sh` (idempotent, master-admin auth) applies this to a **running** realm: removes `user` from default roles if present (guaranteeing the gate), then enables registration. Hardened: container auto-discovery errors on zero/multiple matches; isolated `--config` token cleanup via `trap`; clear auth-failure fallback (exit 2).
- Note: defining the default-roles composite in the import file breaks Keycloak import (`Unable to find composite realm role: uma_authorization`), so default-role state is managed via API, not the import.

### 2. Frontend — pending-approval screen (`components/AuthProvider.tsx`)
After Keycloak auth, read the token roles (`roles` / `realm_access.roles`); if no `admin`/`user` role, render a "pending approval" screen (with a *Check again* button that force-refreshes the token + reloads, and Logout) instead of the app.

### 3. Frontend — admin approval UX (`pages/Users.tsx`, `Users.css`)
- Role-less users show a *Pending* badge (`role-badge--pending`) instead of `—`.
- Role-assignment (and create) default to `user` so an admin never accidentally grants `admin` from the default selection.
- Approval reuses the existing role-change flow.

### 4. i18n (`locales/en.json`, `locales/ko.json`)
- `pending.*` (title/message/account/recheck) for the wait screen; `users.pending` badge label.

### 5. Docs (`README.md`)
Self-registration section rewritten to the approval model (flow, security posture, helper usage, disable).

## Security posture

Open registration only creates **pending** accounts with no access and no API keys, so mass/bot registration is limited to Keycloak user-row growth (never approved). `bruteForceProtected` covers login. reCAPTCHA / network restriction are optional extras, not the access gate.

## Verification

- **realm-export import** (throwaway Keycloak 26.0): `registrationAllowed`/`bruteForceProtected` = true after import; `default-roles-apihub` auto-created without `user`.
- **helper script**: enables registration and removes `user` from default roles; idempotent (re-run exit 0); container discovery (zero/single/multiple, `-db` excluded); auth-failure path (exit 2, no success line); `docker exec` exit propagation confirmed.
- **frontend**: build/typecheck + Vitest (incl. updated Users no-role/pending test).

## Out of scope (YAGNI)

reCAPTCHA, email verification/SMTP, custom Keycloak SPI, a dedicated "pending users" admin queue (the Users page already surfaces them).
