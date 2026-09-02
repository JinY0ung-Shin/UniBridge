# llm-converter (FastAPI sidecar)

See the repo-root `CLAUDE.md` for cross-service context. Tiny stateless service that sits
between APISIX and LiteLLM and translates two API shapes into chat/completions.
Deps: fastapi + httpx only. Test: `pytest` (in `tests/`).

## What it does
- `POST /v1/messages`  — Anthropic Messages API ↔ chat/completions (`messages_bridge.py`).
- `POST /v1/responses` — OpenAI Responses API ↔ chat/completions (`responses_bridge.py`),
  with `previous_response_id` chaining via `responses_state.py` (in-memory state — not durable).
- `GET /v1/models`   — LiteLLM's listing, each model advertised a second time as
  `claude/<id>`: Claude Code's discovery keeps only ids containing `claude`/`anthropic`
  (substring since v2.1.223, prefix-only before), so no bare deployment name survives its
  filter. Every entry carries both the OpenAI (`object`/`created`/`owned_by`) and Anthropic
  (`type`/`display_name`/`created_at`) field sets; `id` + `display_name` are the two the
  client actually requires. Both bridges strip the prefix back off inbound, so an aliased id
  is callable. One LiteLLM hop only — the client's discovery timeout is ~3s.
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
- `CONVERTER_MODEL_ALIAS_PREFIX` (default `claude/`, empty disables both halves) — the prefix
  `/v1/models` advertises aliases under and both bridges strip inbound. Stripping happens right
  after body parsing, before the request builders, so the mid-system model gate and the outbound
  body see the real deployment name. A model actually named `claude/...` upstream is shadowed by
  its own alias — don't register one that way.
- `CONVERTER_REASONING_EFFORT_LEVELS` (default `low,medium,high`, `*` = passthrough) — the
  backend's effort vocabulary. Both bridges forward the client's effort verbatim (via
  `allowed_openai_params`), and vLLM/SGLang 400 on anything else, while Codex's ladder reaches
  `xhigh`/`max`/`ultra`. `reasoning_effort.py` clamps a ladder value to the nearest listed level
  (ties → the cheaper one) and drops an unknown name, so `reasoning_effort` is simply omitted.
- `CONVERTER_LENGTH_AS_COMPLETED` (`auto`|`true`|`false`, default `auto`) — report a
  `finish_reason=length` truncation as terminal `response.completed` rather than the
  spec-correct `response.incomplete`. Codex CLI reads `incomplete` as a failed stream and
  re-sends the entire turn up to `stream_max_retries` (5) times, so one truncation costs six.
  `auto` detects Codex from the `originator` / `user-agent` headers (APISIX forwards both) and
  leaves every other client on spec behaviour. Item statuses follow the terminal status.
- `CONVERTER_FLATTEN_NAMESPACE_TOOLS` (default `true`) — Codex bundles its client-side tools
  (`multi_agent_v1` sub-agents: spawn/send_input/wait/close/resume, and `image_gen`) inside a
  Responses `namespace` tool that chat/completions can't represent, so unflattened they're dropped
  and the model reports them missing. When on, `/v1/responses` flattens each namespace's inner
  functions to top-level chat functions on the request and re-stamps the originating `namespace`
  on the returned `function_call` items — Codex routes a call by `{namespace, name}` and a chat
  tool call carries only `function.name` — so Codex sub-agents / `image_gen` are callable through
  the gateway. `false` restores the drop. `/v1/responses` only (Anthropic clients don't send
  namespace tools); the persisted chain transcript stays name-only.
- `response.created` / `response.in_progress` omit the `instructions` + `tools` echo (Codex sends
  ~39 KB of both and reads neither there); terminal events keep the full echo and stay
  spec-complete.
