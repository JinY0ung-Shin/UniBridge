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

`params`, `limit`, and `timeout` are optional. `params`, when present, must be
a JSON object whose keys match the placeholders in the stored query. Parameter
placeholders are database-specific; they do not all use the `:name` form.

Never build SQL by concatenating or interpolating parameter values. UniBridge
passes each `params` value to the configured database driver as a bind value.
The execution envelope is:

- `params`: optional JSON object or `null`.
- `limit`: optional integer greater than or equal to `1`.
- `timeout`: optional integer from `1` through `300`, in seconds.

## Parameter value formats

The HTTP request accepts standard JSON values inside the `params` object:

| JSON value | Example | Binding guidance |
| --- | --- | --- |
| String | `"alice"` | Text values. Also use strings for dates, timestamps, UUIDs, exact decimals, and encoded binary values, with an explicit database cast or parser when needed. |
| Number | `42`, `3.14` | Integer or finite floating-point input. Do not send `NaN` or infinity. |
| Boolean | `true`, `false` | The target expression must accept the database's boolean type or explicitly convert it. |
| Null | `null` | The query must handle SQL/Cypher null semantics. A typed cast may be required when the database cannot infer the parameter type. |
| Array | `[1, 2, 3]` | Accepted by the HTTP API, but list binding and query syntax are database-specific. See the examples below. |
| Object | `{"region": "KR"}` | Accepted by the HTTP API. Direct map/JSON binding is database-specific; otherwise send serialized JSON as a string and parse it in the query. |

JSON has no native date, timestamp, UUID, arbitrary-precision decimal, or
binary type. Use a string representation and make the expected database type
explicit in the query. The top-level `params` value itself must be an object;
a top-level array is invalid.

An Array is one bind value. UniBridge does not turn it into a comma-separated
SQL fragment and does not dynamically create one placeholder per element.
Consequently, portable scalar syntax such as `IN (:ids)` must not be used for
an Array.

## Database-specific placeholders and Arrays

| Database | Placeholder syntax | Array/list guidance |
| --- | --- | --- |
| PostgreSQL | `:name` | Pass a JSON Array and compare with a typed PostgreSQL array, normally `= ANY(CAST(:ids AS bigint[]))`. Do not use `IN (:ids)`. |
| Microsoft SQL Server | `:name` | Direct list binding is not supported by this query path. Send serialized JSON as a string and expand it with `OPENJSON`; do not use `IN (:ids)`. |
| ClickHouse | `{name:Type}` | Use a typed server-side placeholder such as `{ids:Array(UInt64)}` and pass a JSON Array. |
| Neo4j | `$name` | JSON Arrays and Objects map to Cypher lists and maps. Use list expressions such as `u.id IN $ids`. |
| GraphDB | None | Runtime bind parameters are not supported. Omit `params` or send an empty object, and keep any fixed SPARQL literals in the stored template. |

### PostgreSQL Array example

Store a template with explicit element and timestamp types:

```sql
SELECT id, name
FROM users
WHERE id = ANY(CAST(:ids AS bigint[]))
  AND created_at >= CAST(:since AS timestamptz)
```

Execute it with a JSON Array and an ISO-8601 string:

```json
{
  "params": {
    "ids": [1, 2, 3],
    "since": "2026-07-01T00:00:00Z"
  }
}
```

An empty `ids` Array is still a typed PostgreSQL array and matches no IDs.

### Microsoft SQL Server Array example

Send the list as serialized JSON text. In the request below, `ids_json` is a
JSON string, not a JSON Array:

```sql
SELECT u.id, u.name
FROM dbo.users AS u
WHERE u.id IN (
  SELECT TRY_CAST([value] AS bigint)
  FROM OPENJSON(:ids_json)
)
```

```json
{
  "params": {
    "ids_json": "[1,2,3]"
  }
}
```

`OPENJSON` requires SQL Server compatibility level 130 or later. Validate or
cast each expanded value to the expected database type.

### ClickHouse Array example

Put the ClickHouse type in the placeholder so the driver and server agree on
the element type:

```sql
SELECT user_id, event_name
FROM events
WHERE user_id IN {ids:Array(UInt64)}
```

```json
{
  "params": {
    "ids": [1, 2, 3]
  }
}
```

### Neo4j list example

Use Cypher's `$name` placeholder and pass a JSON Array:

```cypher
MATCH (u:User)
WHERE u.id IN $ids
RETURN u.id, u.name
```

```json
{
  "params": {
    "ids": [1, 2, 3]
  }
}
```

## Parameter authoring rules

- Treat the stored query as the source of truth for placeholder syntax and
  expected database types.
- Supply every placeholder used by the query and avoid unrelated keys; missing
  or extra values may be rejected by the database driver.
- Add explicit casts or typed placeholders for Arrays, nulls, timestamps, and
  other values whose type cannot be inferred safely.
- Describe the contract in the template description, for example:
  `params: ids=array<int64>, since=ISO-8601 timestamp string`.
- Generated OpenAPI can expose detected parameter names, but it does not encode
  the exact type contract for every database. Inspect the stored SQL and
  description before executing a template.
- Never place untrusted values directly into saved SQL. Bind parameters protect
  values, not dynamic table names, column names, keywords, or SQL fragments.

## Query result format

Tabular queries return column names and row Arrays. Values in each row use the
same position as the corresponding name in `columns`:

```json
{
  "columns": ["id", "name", "tags"],
  "rows": [
    [1, "alice", ["admin", "active"]]
  ],
  "row_count": 1,
  "truncated": false,
  "elapsed_ms": 12,
  "graph": null
}
```

- `columns`: Array of column-name strings.
- `rows`: Array of row Arrays; cell values may be JSON nulls, scalars, Arrays,
  or Objects after database-specific serialization.
- `row_count`: number of rows included in this response.
- `truncated`: `true` when the row limit stopped the complete result from being
  returned.
- `elapsed_ms`: execution duration in milliseconds.
- `graph`: serialized graph text for GraphDB `CONSTRUCT`/`DESCRIBE` results;
  otherwise `null`. For a graph result, `columns` and `rows` are empty and
  `row_count` is `0`.

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
