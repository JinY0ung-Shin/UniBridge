#!/usr/bin/env python3
"""Turn captured LiteLLM traffic into a training-ready chat dataset.

What it does
------------
``litellm/custom_callbacks.py`` appends one JSON line per successful LLM call to
the ``litellm-dataset`` volume. Those lines are raw traffic, not a dataset: a
multi-turn chat resends its whole history on every turn, so an N-turn
conversation shows up as N records where each one is a prefix of the next. This
script reads the capture, throws away every superseded prefix so only *final*
conversations survive, appends the assistant reply to make each example
complete, and writes one ``{"messages": [...]}`` line per example.

Getting the data off the volume
-------------------------------
::

    docker compose cp litellm:/var/lib/litellm-dataset ./captured

Example invocations
-------------------
::

    # everything captured so far
    ./scripts/build_finetune_dataset.py --input ./captured --output train.jsonl

    # one model family, last month only, no tool-calling examples
    ./scripts/build_finetune_dataset.py \
        --input ./captured/dataset-20260801.jsonl \
        --input ./captured/dataset-20260802.jsonl \
        --model 'gpt-5*' --exclude-model '*-mini' \
        --since 2026-08-01 --until 2026-08-31 \
        --no-tools --min-turns 2 \
        --output train.jsonl

    # keep provenance for eyeballing / slicing later
    ./scripts/build_finetune_dataset.py -i ./captured -o train.jsonl --include-meta

Capture schema it reads (``schema: 1``)
---------------------------------------
Per line: ``schema``, ``id``, ``trace_id``, ``ts`` (ISO8601 UTC), ``model``,
``end_user``, ``call_type``, ``stream``, ``messages`` (request messages
verbatim), ``response`` (raw provider response), ``prompt_tokens``,
``completion_tokens``, ``cost``. Records with a different ``schema`` are
skipped rather than guessed at. The assistant turn is read from
``response["choices"][0]["message"]``.

Output
------
JSONL, one training example per line, exactly ``{"messages": [...]}`` — our
capture metadata is stripped so the file can be handed to a fine-tuning API
as-is. ``--include-meta`` adds ``model``, ``end_user`` and ``ts`` for
provenance. A stats block goes to stderr so stdout/output stay clean.

Stdlib only, python3.10+: this runs wherever the captured files are, with no
install step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, time, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterator

# Must match litellm/custom_callbacks.py::SCHEMA_VERSION.
SCHEMA_VERSION = 1


class Stats:
    """Per-stage counters, reported at the end so a run is auditable.

    Every drop in the pipeline lands in exactly one counter; the numbers are
    meant to be read as a funnel from ``lines_parsed`` down to ``written``.
    """

    def __init__(self) -> None:
        self.files = 0
        self.lines_parsed = 0
        self.lines_bad_json = 0
        self.skipped_schema = 0
        self.skipped_no_messages = 0
        self.skipped_no_response = 0
        self.skipped_empty_assistant = 0
        self.dropped_by_filters = 0
        self.dropped_bad_ts = 0
        self.groups = 0
        self.exact_duplicates = 0
        self.prefix_superseded = 0
        self.dropped_tools = 0
        self.dropped_min_turns = 0
        self.dropped_empty_after_strip = 0
        self.images_stripped = 0
        self.written = 0


# --------------------------------------------------------------------------- #
# Stage 1: read
# --------------------------------------------------------------------------- #


def collect_input_files(inputs: list[str]) -> list[Path]:
    """Expand ``--input`` arguments (files or directories) to a file list.

    Directories are searched recursively because ``docker compose cp`` drops
    the capture inside a nested folder. Duplicate paths collapse so passing
    both a directory and one of its files cannot double-count records.
    """
    files: list[Path] = []
    seen: set[Path] = set()
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            candidates = sorted(path.rglob("*.jsonl"))
        elif path.exists():
            candidates = [path]
        else:
            print(f"warning: input not found, skipping: {path}", file=sys.stderr)
            continue
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(candidate)
    return files


def read_records(files: list[Path], stats: Stats) -> Iterator[dict[str, Any]]:
    """Yield usable capture records, counting everything dropped on the way.

    A capture file is written by a live proxy, so the last line may be torn or
    a line may hold something unexpected; unparseable lines are counted and
    skipped instead of aborting the build.
    """
    for path in files:
        stats.files += 1
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                stats.lines_parsed += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    stats.lines_bad_json += 1
                    continue
                if not isinstance(record, dict) or record.get("schema") != SCHEMA_VERSION:
                    stats.skipped_schema += 1
                    continue

                messages = record.get("messages")
                if not isinstance(messages, list) or not messages:
                    stats.skipped_no_messages += 1
                    continue

                response = record.get("response")
                if isinstance(response, str):
                    # The capture uses json.dumps(default=str), so a response
                    # object it could not serialize may have become a string.
                    try:
                        response = json.loads(response)
                    except json.JSONDecodeError:
                        response = None
                if not isinstance(response, dict):
                    stats.skipped_no_response += 1
                    continue

                yield {
                    "messages": messages,
                    "response": response,
                    "model": record.get("model"),
                    "end_user": record.get("end_user"),
                    "ts": record.get("ts"),
                }


# --------------------------------------------------------------------------- #
# Stage 2: extract the assistant turn
# --------------------------------------------------------------------------- #


def extract_assistant_message(response: dict[str, Any]) -> dict[str, Any] | None:
    """Pull ``choices[0].message`` out of a raw response.

    Returns None when there is nothing to learn from: no message, or empty
    content with no tool calls (a refusal-shaped or truncated-at-zero-tokens
    reply teaches the model nothing).
    """
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None

    content = message.get("content")
    has_text = bool(text_of(content).strip())
    has_tool_calls = bool(message.get("tool_calls")) or bool(message.get("function_call"))
    if not has_text and not has_tool_calls:
        return None
    return message


def text_of(content: Any) -> str:
    """Flatten message content (string or parts list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text") or ""
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


# --------------------------------------------------------------------------- #
# Stage 3: filters
# --------------------------------------------------------------------------- #


def parse_day_bound(value: str, end_of_day: bool) -> datetime:
    """Parse a ``YYYY-MM-DD`` CLI bound into a UTC datetime.

    Both bounds are inclusive whole days: ``--until 2026-08-31`` keeps calls
    made at 23:59 on the 31st, which is what anyone typing a date means.
    """
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None
    edge = time.max if end_of_day else time.min
    return datetime.combine(day, edge, tzinfo=timezone.utc)


def record_timestamp(record: dict[str, Any]) -> datetime | None:
    """Parse a record's ``ts`` to an aware UTC datetime, or None if unusable."""
    raw = record.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def passes_filters(record: dict[str, Any], args: argparse.Namespace, stats: Stats) -> bool:
    """Apply the CLI selection flags (model / end-user / date window)."""
    model = record.get("model") or ""
    if args.model and not any(fnmatch(model, pattern) for pattern in args.model):
        return False
    if args.exclude_model and any(fnmatch(model, pattern) for pattern in args.exclude_model):
        return False
    if args.end_user and record.get("end_user") not in args.end_user:
        return False

    if args.since or args.until:
        ts = record_timestamp(record)
        if ts is None:
            # A date window was requested and this record cannot answer it —
            # excluding it is the honest choice.
            stats.dropped_bad_ts += 1
            return False
        if args.since and ts < args.since:
            return False
        if args.until and ts > args.until:
            return False
    return True


# --------------------------------------------------------------------------- #
# Stage 4: prefix dedup to final turns
# --------------------------------------------------------------------------- #


def chain_digests(messages: list[Any]) -> list[str]:
    """Rolling hash chain over a message list: ``chain[i]`` covers messages 0..i.

    ``h_i = sha256(h_{i-1} + canonical(msg_i))``, so two message lists share a
    chain value at position i exactly when their first i+1 messages are equal.
    That turns "is A a prefix of B" into a dict lookup, and building every
    chain costs one hash per message — O(total messages) for the whole run
    rather than the O(n^2) pairwise comparison the naive version would do.
    """
    digests: list[str] = []
    running = ""
    for message in messages:
        canonical = json.dumps(message, sort_keys=True, ensure_ascii=False, default=str)
        running = hashlib.sha256((running + canonical).encode("utf-8")).hexdigest()
        digests.append(running)
    return digests


def drop_superseded_prefixes(
    records: list[dict[str, Any]], stats: Stats
) -> list[dict[str, Any]]:
    """Keep only the final turn of each conversation.

    Grouped by ``(end_user, model)``: a conversation never spans users, and a
    model switch mid-thread starts a new lineage worth keeping on its own.

    Within a group, a record is dropped when its full message list is a *strict*
    prefix of some other record's — i.e. an earlier turn of the same
    conversation, already contained in the later one. Indexing only positions
    before each record's own length means a record can never supersede itself,
    and exact duplicates (same messages, e.g. a client retry) are collapsed
    beforehand so they cannot cancel each other out.
    """
    groups: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault((record.get("end_user"), record.get("model")), []).append(record)
    stats.groups = len(groups)

    kept: list[dict[str, Any]] = []
    for group in groups.values():
        unique: list[dict[str, Any]] = []
        seen_full: set[str] = set()
        for record in group:
            digests = chain_digests(record["messages"])
            full = digests[-1]
            if full in seen_full:
                stats.exact_duplicates += 1
                continue
            seen_full.add(full)
            record["_chain"] = digests
            unique.append(record)

        # Every strict-prefix digest in the group: each record contributes all
        # of its chain values except the last (which is the record itself).
        strict_prefixes: set[str] = set()
        for record in unique:
            strict_prefixes.update(record["_chain"][:-1])

        for record in unique:
            if record["_chain"][-1] in strict_prefixes:
                stats.prefix_superseded += 1
                continue
            kept.append(record)
    return kept


# --------------------------------------------------------------------------- #
# Stages 5-6: assemble + content hygiene
# --------------------------------------------------------------------------- #


def strip_non_text_parts(messages: list[Any]) -> tuple[list[Any], bool, bool]:
    """Reduce parts-list content to its text, dropping images and other media.

    The target is text SFT, so an ``image_url`` part is unusable weight in the
    example; the surrounding text is still valuable and is kept. Returns the
    rewritten messages plus two flags: whether anything was dropped, and
    whether some message was left with no content at all (which makes the whole
    example unusable — the text referred to an image the model will not see).
    """
    out: list[Any] = []
    stripped_any = False
    emptied_any = False
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            out.append(message)
            continue
        parts = message["content"]
        kept_text = [
            part.get("text") or ""
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        kept_text = [text for text in kept_text if text]
        if len(kept_text) != len(parts):
            stripped_any = True
        joined = "\n".join(kept_text)
        if not joined.strip() and parts:
            emptied_any = True
        rewritten = dict(message)
        rewritten["content"] = joined
        out.append(rewritten)
    return out, stripped_any, emptied_any


def uses_tools(messages: list[Any]) -> bool:
    """True if the conversation involves tool calling in either direction."""
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            return True
        if message.get("tool_calls") or message.get("function_call"):
            return True
    return False


def count_user_turns(messages: list[Any]) -> int:
    return sum(
        1 for message in messages if isinstance(message, dict) and message.get("role") == "user"
    )


def build_example(
    record: dict[str, Any], args: argparse.Namespace, stats: Stats
) -> dict[str, Any] | None:
    """Assemble one training example, or None if it fails a hygiene rule."""
    conversation = list(record["messages"]) + [record["assistant"]]
    conversation, stripped, emptied = strip_non_text_parts(conversation)
    if stripped:
        stats.images_stripped += 1
    if emptied:
        stats.dropped_empty_after_strip += 1
        return None
    if args.no_tools and uses_tools(conversation):
        stats.dropped_tools += 1
        return None
    if count_user_turns(conversation) < args.min_turns:
        stats.dropped_min_turns += 1
        return None

    example: dict[str, Any] = {"messages": conversation}
    if args.include_meta:
        example["model"] = record.get("model")
        example["end_user"] = record.get("end_user")
        example["ts"] = record.get("ts")
    return example


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_finetune_dataset.py",
        description="Build a chat fine-tuning dataset from captured LiteLLM traffic.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Get the capture with: docker compose cp litellm:/var/lib/litellm-dataset ./captured",
    )
    parser.add_argument(
        "-i",
        "--input",
        action="append",
        required=True,
        metavar="PATH",
        help="capture file or directory of *.jsonl (repeatable, directories are recursive)",
    )
    parser.add_argument(
        "-o", "--output", required=True, metavar="PATH", help="JSONL file to write"
    )
    parser.add_argument(
        "--model",
        action="append",
        metavar="PATTERN",
        help="keep only these models (fnmatch glob, repeatable)",
    )
    parser.add_argument(
        "--exclude-model",
        action="append",
        metavar="PATTERN",
        help="drop these models (fnmatch glob, repeatable, applied after --model)",
    )
    parser.add_argument(
        "--end-user",
        action="append",
        metavar="VALUE",
        help="keep only these end users (exact match, repeatable)",
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        type=lambda v: parse_day_bound(v, end_of_day=False),
        help="drop calls before this UTC day (inclusive)",
    )
    parser.add_argument(
        "--until",
        metavar="YYYY-MM-DD",
        type=lambda v: parse_day_bound(v, end_of_day=True),
        help="drop calls after this UTC day (inclusive)",
    )
    parser.add_argument(
        "--min-turns",
        type=int,
        default=1,
        metavar="N",
        help="require at least N user turns in the final conversation (default: 1)",
    )
    parser.add_argument(
        "--no-tools",
        action="store_true",
        help="drop examples that involve tool calling (default: keep them)",
    )
    parser.add_argument(
        "--include-meta",
        action="store_true",
        help="also emit model/end_user/ts per example (default: messages only)",
    )
    return parser


def report(stats: Stats, args: argparse.Namespace, output: Path) -> None:
    """Print the funnel to stderr; stdout is left clean for piping.

    Read top to bottom it accounts for every captured line: each drop lands in
    exactly one row, so a surprising dataset size can always be traced to the
    stage that caused it.
    """
    filtered = str(stats.dropped_by_filters)
    if stats.dropped_bad_ts:
        filtered += f" (of which unparseable ts: {stats.dropped_bad_ts})"
    rows = [
        ("files read", stats.files),
        ("lines parsed", stats.lines_parsed),
        ("lines skipped (bad json)", stats.lines_bad_json),
        (f"records skipped (schema != {SCHEMA_VERSION})", stats.skipped_schema),
        ("records skipped (no messages)", stats.skipped_no_messages),
        ("records skipped (no response)", stats.skipped_no_response),
        ("records skipped (empty assistant)", stats.skipped_empty_assistant),
        ("records dropped by filters", filtered),
        ("dedup groups (end_user, model)", stats.groups),
        ("exact duplicates collapsed", stats.exact_duplicates),
        ("prefix-superseded dropped", stats.prefix_superseded),
        ("examples with images stripped", stats.images_stripped),
        ("examples dropped (content emptied)", stats.dropped_empty_after_strip),
        ("examples dropped (--no-tools)", stats.dropped_tools),
        (f"examples dropped (--min-turns {args.min_turns})", stats.dropped_min_turns),
        ("examples written", stats.written),
        ("output", output),
    ]
    width = max(len(label) for label, _ in rows) + 2
    lines = ["=== build_finetune_dataset ==="]
    lines += [f"  {label:<{width}}{value}" for label, value in rows]
    print("\n".join(lines), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stats = Stats()

    files = collect_input_files(args.input)
    if not files:
        print("error: no *.jsonl input files found", file=sys.stderr)
        return 1

    selected: list[dict[str, Any]] = []
    for record in read_records(files, stats):
        assistant = extract_assistant_message(record["response"])
        if assistant is None:
            stats.skipped_empty_assistant += 1
            continue
        if not passes_filters(record, args, stats):
            stats.dropped_by_filters += 1
            continue
        record["assistant"] = assistant
        # Remember arrival order so the output mirrors the capture rather than
        # the grouping used for dedup — easier to eyeball and diff.
        record["_order"] = len(selected)
        selected.append(record)

    finals = drop_superseded_prefixes(selected, stats)
    finals.sort(key=lambda record: record["_order"])

    output = Path(args.output)
    if output.parent != Path(""):
        output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for record in finals:
            example = build_example(record, args, stats)
            if example is None:
                continue
            handle.write(json.dumps(example, ensure_ascii=False, default=str) + "\n")
            stats.written += 1

    report(stats, args, output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
