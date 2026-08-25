# llm-converter (FastAPI sidecar)

See the repo-root `CLAUDE.md` for cross-service context. Tiny stateless service that sits
between APISIX and LiteLLM and translates two API shapes into chat/completions.
Deps: fastapi + httpx only. Test: `pytest` (in `tests/`).

## What it does
- `POST /v1/messages`  — Anthropic Messages API ↔ chat/completions (`messages_bridge.py`).
- `POST /v1/responses` — OpenAI Responses API ↔ chat/completions (`responses_bridge.py`),
  with `previous_response_id` chaining via `responses_state.py` (in-memory state — not durable).
- Streaming: `sse.py` (SSE framing) + `stream_sanitizer.py` (cleans/normalizes upstream chunks).
- `config.py` — upstream LiteLLM URL + key; `main.py` — app + routes.

## Notes
- Request path is `client → UI nginx → APISIX (key-auth, master-key inject) → llm-converter → LiteLLM`.
  Live coverage lives in repo-root `e2e/` (runs only when `LLM_API_KEY` is set), not here.
- Reasoning models emit a `thinking` block before answer text — keep `max_tokens` generous
  when testing or the answer can be empty.
- `CONVERTER_MID_SYSTEM_POLICY` (`user`|`hoist`|`asis`, default `user`) — strict chat templates
  (newer Qwen) 400 on any system message past index 0, and Claude Code sends mid-history
  `role:"system"` reminders. `system_norm.py` merges the leading run + role-swaps later ones;
  both bridges apply it as the last request step (so `/v1/responses` chains stay normalized).
  Gated by `CONVERTER_MID_SYSTEM_MODEL_PATTERN` — case-insensitive regex `search`ed against the
  outbound model (default `qwen3\.\d`, so `qwen3-8b`/`gpt-4o` pass through untouched; `.*` = all).
