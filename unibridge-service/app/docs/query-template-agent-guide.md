# UniBridge Query Template Agent Guide

This guide is available from the gateway at:

`GET /api/query/templates/guide`

Send the UniBridge API key in the APISIX `apikey` header.

## Independent route grants

Query-template reading/execution and editing are separate API-key grants:

- `query-api`: discover and execute templates, and read this guide.
- `query-template-write-api`: edit safe fields on an existing template.

Grant both routes to an agent that must discover, execute, and edit templates.
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

At least one editable field is required. `path`, `name`, `database`, `enabled`,
creation, and deletion remain admin-only. Extra fields are rejected.

Use the `updated_at` value returned by discovery as `expected_updated_at`.
When another user or agent changed the template after discovery, the API returns
`409 Conflict`; fetch the latest template before retrying. Omitting
`expected_updated_at` is supported but does not protect against overwrites.

Edited SQL must remain read-only (`SELECT` or safe `EXPLAIN`) and may reference
only databases and tables allowed by the editing key. These restrictions are
also enforced again when the template is executed.

## Common responses

- `200`: request succeeded.
- `400`: SQL is not a read-only template or the path is invalid.
- `403`: required route, database, or table access is missing.
- `404`: template or database does not exist.
- `409`: `expected_updated_at` is stale.
- `422`: request fields are missing, invalid, or not editable by an agent.
