"""Guards on ``litellm/config.yaml``, the proxy config bind-mounted at deploy.

The file is not exercised by any other test, yet a silent edit to it changes
how every LLM call behaves in production (timeouts, retries, logging).

Run from the repo root: ``python -m pytest litellm/tests/``.
"""

from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

CONFIG_FILE = pathlib.Path(__file__).resolve().parent.parent / "config.yaml"

# The APISIX LLM routes read-timeout upstream responses at 600s
# (unibridge-service/app/main.py), so LiteLLM waiting longer is pointless.
GATEWAY_READ_TIMEOUT_SECONDS = 600


@pytest.fixture(scope="module")
def config() -> dict:
    loaded = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), CONFIG_FILE
    return loaded


def test_request_timeout_is_bounded_by_the_gateway_read_timeout(config) -> None:
    request_timeout = config["litellm_settings"]["request_timeout"]

    # bool is an int subclass; a `true` here would silently mean 1 second.
    assert isinstance(request_timeout, int) and not isinstance(request_timeout, bool)
    assert 0 < request_timeout <= GATEWAY_READ_TIMEOUT_SECONDS


def test_timed_out_requests_are_not_retried(config) -> None:
    # A request that already burned request_timeout must not be re-sent: the
    # backend is saturated, and num_retries would put three copies on it.
    retry_policy = config["router_settings"]["retry_policy"]

    assert retry_policy["TimeoutErrorRetries"] == 0
    # Other error classes (connection-level failures) still deserve a retry.
    assert "num_retries" in config["litellm_settings"]


def test_logging_bypass_stays_closed_and_metrics_stay_on(config) -> None:
    litellm_settings = config["litellm_settings"]

    # Without this a caller can pass `"no-log": true` and skip all logging.
    assert litellm_settings["global_disable_no_log_param"] is True
    # Unsupported params are dropped rather than 400-ing the whole call.
    assert litellm_settings["drop_params"] is True
    assert "prometheus" in litellm_settings["success_callback"]
