"""Load ``litellm/custom_callbacks.py`` once for every test module here.

Two problems this solves, both of which bite at *import* time:

1. ``custom_callbacks.py`` imports ``litellm`` at module load, which may not be
   installed in a dev/CI environment — so a minimal stand-in for
   ``litellm.integrations.custom_logger`` is installed first when the real one
   is missing. The module is then loaded by path, exercising the *real* code.
2. The module registers a Prometheus histogram in the default registry as an
   import side effect, and re-registering a metric name raises. Loading it once
   into ``sys.modules`` means every test module shares the one copy (and the one
   registration) regardless of collection order.

Test modules use it as ``import custom_callbacks_under_test as cc``.

Run from the repo root: ``python -m pytest litellm/tests/``.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import tempfile
import types

MODULE_NAME = "custom_callbacks_under_test"


def _install_litellm_stub() -> None:
    try:  # pragma: no cover - depends on the environment
        import litellm.integrations.custom_logger  # noqa: F401

        return
    except Exception:
        pass

    _litellm = sys.modules.setdefault("litellm", types.ModuleType("litellm"))
    _integ = sys.modules.setdefault(
        "litellm.integrations", types.ModuleType("litellm.integrations")
    )
    _cl = types.ModuleType("litellm.integrations.custom_logger")

    class CustomLogger:  # minimal base: the module only subclasses it
        def __init__(self, *args, **kwargs):
            pass

    _cl.CustomLogger = CustomLogger
    _litellm.integrations = _integ
    _integ.custom_logger = _cl
    sys.modules["litellm.integrations.custom_logger"] = _cl


def _load_module_under_test():
    existing = sys.modules.get(MODULE_NAME)
    if existing is not None:
        return existing

    _install_litellm_stub()
    # The module instantiates a logger at import; point its dir at a throwaway
    # temp location so import never touches the default /var/lib path.
    os.environ["LITELLM_DATASET_DIR"] = tempfile.mkdtemp(
        prefix="litellm-dataset-import-"
    )

    path = pathlib.Path(__file__).resolve().parent.parent / "custom_callbacks.py"
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so a re-entrant import finds this copy, not a second.
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


_load_module_under_test()
