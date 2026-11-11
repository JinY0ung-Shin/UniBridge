"""Context windows read from Shepherd Model Gateway's ``GET /workers``.

Some OpenAI-compatible gateways list only model ids on ``/v1/models``. SMG
reports each model's limit per worker instead::

    {"workers": [{"is_healthy": true, "status": "ready",
                  "models": [{"id": "Qwen/Qwen3-8B", "context_length": 131072}]}]}

Only a model id and a positive ``context_length`` are taken from that body.
Worker URLs, labels, tokenizer and checkpoint paths stay here, and the body is
remote input: an entry of the wrong shape is skipped, never an error.
"""

from __future__ import annotations


class MalformedWorkers(ValueError):
    """The body is not a worker listing at all (no ``workers`` list)."""


def _serves_traffic(worker: dict) -> bool:
    """Healthy and routable: SMG keeps a worker that is draining, not ready or
    stalled in the listing, but sends it no requests."""
    if worker.get("is_healthy") is not True:
        return False
    status = worker.get("status")  # absent on SMG builds older than the field
    if status is not None and status != "ready":
        return False
    return worker.get("stalled") is None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def context_lengths(payload: object) -> tuple[dict[str, int], list[str]]:
    """``({model id: context_length}, [ids whose workers disagree])``.

    A model is in the mapping when every serving worker that reports a limit
    for it reports the same one. Workers that disagree put the id in the second
    list and leave it out of the mapping: any single value could be more than
    another worker accepts.
    """
    workers = payload.get("workers") if isinstance(payload, dict) else None
    if not isinstance(workers, list):
        raise MalformedWorkers("no workers list")

    reported: dict[str, set[int]] = {}
    for worker in workers:
        if not isinstance(worker, dict) or not _serves_traffic(worker):
            continue
        models = worker.get("models")
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            length = _positive_int(model.get("context_length"))
            if isinstance(model_id, str) and model_id and length is not None:
                reported.setdefault(model_id, set()).add(length)

    lengths = {model_id: next(iter(values)) for model_id, values in reported.items() if len(values) == 1}
    conflicts = sorted(model_id for model_id, values in reported.items() if len(values) > 1)
    return lengths, conflicts
