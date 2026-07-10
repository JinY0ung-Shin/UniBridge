# UniBridge Query Template Agent Guide

This guide is available from the gateway at:

`GET /api/query/templates/guide`

Send the UniBridge API key in the APISIX `apikey` header.

## Independent route grants

Query-template reading/execution and editing are separate API-key grants:

- `query-api`: discover and execute templates, and read this guide.
- `query-template-write-api`: create, edit, and delete accessible templates.

Grant both routes to an agent that must discover, execute, and manage templates.
Grant only `query-api` to a read/execute-only agent. The database and table
allowlists on the API key apply in addition to these route grants.

## Discover templates

```http
GET /api/query/templates
apikey: <API_KEY>
```

The response contains enabled templates that the key can execute. Each item
includes its stable `path`, SQL, database, limits, and `updated_at` value.

## Execute a template

```http
POST /api/query/templates/reports/users
Content-Type: application/json
apikey: <API_KEY>

{
  "params": {"id": 42},
  "limit": 50,
  "timeout": 30
}
```

`params`, `limit`, and `timeout` are optional. Named SQL parameters use the
`:name` form.

## Create a template

Choose the stable path in the URL and send the template definition with `PUT`:

```http
PUT /api/query/templates/reports/new-users
Content-Type: application/json
apikey: <API_KEY>

{
  "name": "New users report",
  "description": "List recently created users",
  "database": "maindb",
  "sql": "SELECT id, name FROM users WHERE created_at >= :since",
  "default_limit": 50,
  "timeout": 30
}
```

`name`, `database`, and `sql` are required. New agent-created templates are
always enabled. The path must not already exist, and the database, SQL, and
referenced tables must be within the API key's access scope. Slash separates
path segments; each segment may contain letters, digits, `.`, `_`, and `-`, and
the complete path may be at most 200 characters. The `201` response contains
the created template and its initial `updated_at` value.

## Edit a template

```http
PATCH /api/query/templates/reports/users
Content-Type: application/json
apikey: <API_KEY>

{
  "sql": "SELECT id, name FROM users WHERE id = :id",
  "description": "Look up one user by id",
  "default_limit": 50,
  "timeout": 30,
  "expected_updated_at": "2026-07-10T00:00:00Z"
}
```

The editable fields are:

- `sql`
- `description`
- `default_limit` (`null` clears it)
- `timeout` (`null` clears it)

At least one editable field is required. `path`, `name`, `database`, and
`enabled` cannot be changed through PATCH. Extra fields are rejected.

Use the `updated_at` value returned by discovery as `expected_updated_at`.
When another user or agent changed the template after discovery, the API returns
`409 Conflict`; fetch the latest template before retrying. Omitting
`expected_updated_at` is supported but does not protect against overwrites.

Edited SQL must remain read-only (`SELECT` or safe `EXPLAIN`) and may reference
only databases and tables allowed by the editing key. These restrictions are
also enforced again when the template is executed.

## Delete a template

First discover the template and copy its current `updated_at` value. Deletion
requires that value so a stale agent cannot remove a template changed by
someone else:

```http
DELETE /api/query/templates/reports/new-users?expected_updated_at=2026-07-10T00:00:00Z
apikey: <API_KEY>
```

A successful deletion returns `204 No Content`. If the template changed after
discovery, the API returns `409 Conflict`; fetch the latest list and decide
again instead of blindly retrying the deletion. Deletion removes the saved
template; there is no agent undo endpoint.

## Common responses

- `200`: request succeeded.
- `201`: template created.
- `204`: template deleted.
- `400`: SQL is not a read-only template or the path is invalid.
- `403`: required route, database, or table access is missing.
- `404`: template or database does not exist.
- `409`: the create path already exists or `expected_updated_at` is stale.
- `422`: request fields are missing, invalid, or not editable by an agent.
