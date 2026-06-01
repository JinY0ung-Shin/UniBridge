"""Throwaway diagnostic: dump what a /v1/messages stream actually emits.

Reuses conftest config (.env is auto-loaded there). Run from e2e/:
    python _diag_stream.py
Prints the event-type histogram, delta-type breakdown, any text/thinking
captured, and the terminal stop_reason — so we can see whether the model
produced only a thinking block (truncated) or the bridge mislabeled deltas.
"""

from __future__ import annotations

import collections
import json

import httpx

import conftest as c

PROMPT = "Count to three."


def main() -> None:
    if not c.API_KEY:
        raise SystemExit("set LLM_API_KEY (or e2e/.env) first")
    model = c.MODEL
    headers = {c.API_KEY_HEADER: c.API_KEY, "content-type": "application/json"}
    with httpx.Client(base_url=c.BASE_URL, verify=c.TLS_VERIFY, timeout=c.TIMEOUT) as client:
        if not model:
            r = client.get("/v1/models", headers=headers)
            r.raise_for_status()
            model = (r.json().get("data") or [{}])[0].get("id", "")
        print(f"model={model!r} max_tokens={c.MAX_TOKENS}")

        body = {
            "model": model,
            "max_tokens": c.MAX_TOKENS,
            "stream": True,
            "messages": [{"role": "user", "content": PROMPT}],
        }
        evt_types: collections.Counter = collections.Counter()
        delta_types: collections.Counter = collections.Counter()
        text_buf, think_buf = [], []
        stop_reason = None
        last_event = None
        with client.stream("POST", "/v1/messages", headers=headers, json=body) as resp:
            print(f"status={resp.status_code} ctype={resp.headers.get('content-type')}")
            cur = None
            for raw in resp.iter_lines():
                line = raw if isinstance(raw, str) else raw.decode()
                line = line.rstrip("\r")
                if line == "":
                    cur = None
                    continue
                if line.startswith("event:"):
                    cur = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        continue
                    try:
                        d = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    et = cur or (d.get("type") if isinstance(d, dict) else None)
                    evt_types[et] += 1
                    last_event = (et, d)
                    if et == "content_block_delta":
                        dt = (d.get("delta") or {}).get("type")
                        delta_types[dt] += 1
                        if dt == "text_delta":
                            text_buf.append(d["delta"].get("text", ""))
                        elif dt == "thinking_delta":
                            think_buf.append(d["delta"].get("thinking", ""))
                    if et == "message_delta":
                        stop_reason = (d.get("delta") or {}).get("stop_reason", stop_reason)

        print("event types:", dict(evt_types))
        print("delta types:", dict(delta_types))
        print("stop_reason:", stop_reason)
        print(f"thinking chars: {sum(len(x) for x in think_buf)}")
        print(f"text chars:     {sum(len(x) for x in text_buf)}")
        print("text sample:", repr("".join(text_buf)[:200]))
        print("last event:", json.dumps(last_event, ensure_ascii=False)[:400])


if __name__ == "__main__":
    main()
