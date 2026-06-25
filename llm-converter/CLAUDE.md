# llm-converter (FastAPI sidecar)

See the repo-root `CLAUDE.md` for cross-service context. Tiny stateless service that sits
between APISIX and Bifrost and translates two API shapes into chat/completions.
Deps: fastapi + httpx only. Test: `pytest` (in `tests/`).

## What it does
- `POST /v1/messages`  — Anthropic Messages API ↔ chat/completions (`messages_bridge.py`).
- `POST /v1/responses` — OpenAI Responses API ↔ chat/completions (`responses_bridge.py`),
  with `previous_response_id` chaining via `responses_state.py` (in-memory state — not durable).
- Streaming: `sse.py` (SSE framing) + `stream_sanitizer.py` (cleans/normalizes upstream chunks).
- `config.py` — upstream LLM gateway URL; `main.py` — app + routes.

## Notes
- Request path is `client → UI nginx → APISIX (key-auth, Bifrost headers) → llm-converter → Bifrost`.
  Live coverage lives in repo-root `e2e/` (runs only when `LLM_API_KEY` is set), not here.
- Reasoning models emit a `thinking` block before answer text — keep `max_tokens` generous
  when testing or the answer can be empty.
