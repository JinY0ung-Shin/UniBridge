# Live E2E tests — `/v1/messages` & `/v1/responses`

End-to-end tests that validate the LLM converter against a **running** UniBridge
deployment. They drive the real request path:

```
client → UI nginx (:UI_PORT) → APISIX (key-auth, Bifrost headers) → llm-converter → Bifrost
```

so a green run confirms routing, authentication, the Anthropic/Responses ↔
chat-completions translation, and streaming all work together against a real model.

These are **not** part of the unit suites (different directory, and skipped
unless `LLM_API_KEY` is set).

## Prerequisites

- The stack is up (`docker compose up -d`) and healthy.
- An APISIX API key exists and is granted LLM access (so its consumer is
  whitelisted on `llm-messages` / `llm-responses` — granting `llm-proxy` is
  enough; access is aliased). Create one via the UI (API Keys) or the admin API.
- At least one model is configured in Bifrost.

## Configure

| Env var | Default | Notes |
|---|---|---|
| `LLM_API_KEY` | — | **Required.** APISIX consumer key. Tests skip if unset. |
| `LLM_BASE_URL` | `https://localhost:3000/api/llm` | Gateway base incl. the `/api/llm` prefix. |
| `LLM_API_KEY_HEADER` | `apikey` | APISIX key-auth header. |
| `LLM_MODEL` | (auto) | Model id. If unset, discovered from `GET /v1/models`. |
| `LLM_TLS_VERIFY` | `false` | `true` / `false` / path to CA bundle (UI uses a self-signed cert). |
| `LLM_TIMEOUT` | `60` | Per-request timeout (seconds). |
| `LLM_MAX_TOKENS` | `4096` | `max_tokens` for `/v1/messages`. Keep generous — reasoning models spend budget on a `thinking` block before any answer text. |

Config can come from real environment variables **or** an `e2e/.env` file
(loaded automatically; gitignored). Real env vars override `.env` entries.

## Run

```bash
cd e2e
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Option A — use a .env file
cp .env.example .env
# edit .env: set LLM_API_KEY (and LLM_BASE_URL / LLM_MODEL as needed)

# Option B — export inline (overrides .env)
export LLM_BASE_URL="https://your-host:3000/api/llm"
export LLM_API_KEY="<apisix-consumer-key>"
export LLM_MODEL="<model-id>"        # optional; auto-discovered otherwise
export LLM_TLS_VERIFY=false          # self-signed UI cert

pytest -v
```

If `LLM_API_KEY` is unset the suite reports all tests as skipped — safe to leave
wired into CI behind a deployment-gated job.

## What is covered

- **messages**: non-streaming (Anthropic `message` shape), streaming
  (`message_start … message_stop` with `text_delta`), and that APISIX rejects an
  unauthenticated call (401/403).
- **responses**: non-streaming (`object:"response"`, `output[]`, usage),
  streaming (`response.created … response.completed`, monotonic `sequence_number`,
  `output_text.delta`, full terminal `output[]`), `previous_response_id`
  chaining, and unknown-id → `400 previous_response_not_found`.
