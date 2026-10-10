# llm-converter (FastAPI sidecar)

See the repo-root `CLAUDE.md` for cross-service context. Tiny stateless service that sits
between APISIX and the LLM gateway — LiteLLM or Bifrost, picked by `LLM_GATEWAY` — and
translates two API shapes into chat/completions.
Deps: fastapi + httpx only. Test: `pytest` (in `tests/`).

## What it does
- `POST /v1/messages`  — Anthropic Messages API ↔ chat/completions (`messages_bridge.py`).
- `POST /v1/responses` — OpenAI Responses API ↔ chat/completions (`responses_bridge.py`),
  with `previous_response_id` chaining via `responses_state.py` (in-memory state — not durable).
- `GET /v1/models`   — the gateway's listing, each model advertised a second time as
  `claude/<id>`: Claude Code's discovery keeps only ids containing `claude`/`anthropic`
  (substring since v2.1.223, prefix-only before), so no bare deployment name survives its
  filter. Every entry carries both the OpenAI (`object`/`created`/`owned_by`) and Anthropic
  (`type`/`display_name`/`created_at`) field sets; `id` + `display_name` are the two the
  client actually requires. Both bridges strip the prefix back off inbound, so an aliased id
  is callable. The listing is rebuilt from upstream `data` alone: Bifrost's `key_statuses`
  (key ids, failing backends' raw errors with internal host names) and `extra_fields` never
  reach the client, and Bifrost's `<provider>/<id>` ids lose the provider segment (deduped)
  so clients see the bare names they call. Cached process-wide — APISIX injects one shared
  credential, so the listing does not depend on the caller — for `CONVERTER_MODELS_CACHE_TTL`
  (30s). Because the fetch is shared it carries ONLY that credential (`Authorization` on
  LiteLLM, `x-bf-vk` on Bifrost): LiteLLM prefers a client's `x-litellm-api-key` to
  `Authorization`, which would hand every joined caller that key's listing or 401.
  Concurrent misses share one fetch, and a caller waits `CONVERTER_MODELS_TIMEOUT` (Bifrost
  2s — it asks every key live with no deadline, and Claude Code gives up after ~3s; LiteLLM
  30s, the pre-cache wait; an explicit value applies to both) before getting the last good
  listing (if younger than `CONVERTER_MODELS_STALE_MAX`, 600s) or a 504; the fetch keeps
  going and fills the cache. Timeouts, unreachable and 5xx fall back to the last listing;
  4xx do not.
- `POST /v1/messages/count_tokens` — answered locally (`token_estimate.py`), never upstream:
  no gateway counts for a self-hosted model. A deliberately high estimate (ASCII ~3 chars per
  token — 4 measured 0.77-0.87 of Qwen3's real count — other characters 1 each, flat per
  image/document, per-turn and per-tool overhead), because clients compact on it. A value of
  the wrong type is a 400 like `/v1/messages`'.
- Non-2xx JSON error bodies on `/v1/messages` and `/v1/responses` are forwarded with their
  status but without Bifrost's top-level `extra_fields` (provider, key name, backend model id);
  non-JSON bodies pass unchanged.
- Streaming: `sse.py` (SSE framing, header filters) + `stream_sanitizer.py` (cleans/normalizes
  upstream chunks).
- `config.py` — upstream gateway URL + knobs; `main.py` — app + routes.

## Notes
- Request path is `client → UI nginx → APISIX (key-auth, credential inject) → llm-converter →
  LiteLLM or Bifrost`. Live coverage lives in repo-root `e2e/` (runs only when `LLM_API_KEY`
  is set), not here.
- `LLM_GATEWAY` (`litellm`|`bifrost`, default `litellm`, unknown → `litellm`) — the same
  switch unibridge-service reads for the APISIX routes. `bifrost` sends to
  `CONVERTER_BIFROST_URL` (default `http://bifrost:8080`, plain HTTP) and needs no
  `LITELLM_URL`. APISIX sets BOTH gateways' credentials on the converter routes (a deploy
  re-points routes before the color promotion flips this upstream), so `sse.py`
  `forward_request_headers` passes only the active one's: litellm → `Authorization` and
  `x-litellm-*`, no `x-bf-*` at all; bifrost → only `x-bf-vk`/`x-bf-dim-consumer`/
  `x-bf-lh-consumer`, no other `x-bf-*` (raw capture, passthrough, cache, MCP, stored-key
  selectors), no `Authorization`/`x-api-key`/`api-key`/`x-goog-api-key`, no `x-litellm-*`.
  Bifrost's `x-bifrost-*` response headers (provider, key name, fallback) are dropped in both
  modes.
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
  backend's effort vocabulary. Both bridges forward the client's effort verbatim (on LiteLLM via
  `allowed_openai_params`; that field is LiteLLM-only, since Bifrost's built-in vllm/sgl types
  would pass it through to the backend), and vLLM/SGLang 400 on anything else, while Codex's
  ladder reaches `xhigh`/`max`/`ultra`. `reasoning_effort.py` clamps a ladder value to the
  nearest listed level (ties → the cheaper one) and drops an unknown name, so `reasoning_effort`
  is simply omitted. The clamp runs in both modes.
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
