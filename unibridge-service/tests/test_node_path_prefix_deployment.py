"""The custom APISIX plugin behind per-node path prefixes is wired up everywhere.

APISIX honours a node's ``metadata.path_prefix`` only when four pieces agree:
the Lua file exists, ``apisix/config.yaml`` lists the plugin (APISIX loads no
plugin it does not list), every compose file that runs APISIX mounts the file
where APISIX looks for plugins, and unibridge-service turns it on through a
global rule naming the same plugin (``app/services/node_path_prefix.py``).
Missing any one of them makes prefixes silently do nothing, or makes the global
rule PUT fail with "unknown plugin".
"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.services.node_path_prefix import PLUGIN_NAME


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_FILE = REPO_ROOT / "apisix" / "plugins" / f"{PLUGIN_NAME}.lua"
APISIX_CONFIG_FILE = REPO_ROOT / "apisix" / "config.yaml"
APISIX_COMPOSE_FILES = (
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "docker-compose.infra.yml",
)
PLUGIN_MOUNT = (
    f"./apisix/plugins/{PLUGIN_NAME}.lua"
    f":/usr/local/apisix/apisix/plugins/{PLUGIN_NAME}.lua:ro"
)


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_plugin_file_declares_the_name_the_backend_uses() -> None:
    source = PLUGIN_FILE.read_text(encoding="utf-8")
    assert f'local plugin_name = "{PLUGIN_NAME}"' in source


def test_apisix_config_loads_the_plugin() -> None:
    assert PLUGIN_NAME in _load_yaml(APISIX_CONFIG_FILE)["plugins"]


def test_every_apisix_compose_service_mounts_the_plugin() -> None:
    for compose_file in APISIX_COMPOSE_FILES:
        volumes = _load_yaml(compose_file)["services"]["apisix"]["volumes"]
        assert PLUGIN_MOUNT in volumes, compose_file.name


def test_no_other_compose_file_runs_apisix() -> None:
    # A new layout that starts APISIX has to mount the plugin as well; adding it
    # to APISIX_COMPOSE_FILES makes the test above check that.
    running_apisix = sorted(
        path.name
        for path in REPO_ROOT.glob("docker-compose*.yml")
        if "apache/apisix" in path.read_text(encoding="utf-8")
    )
    assert running_apisix == sorted(path.name for path in APISIX_COMPOSE_FILES)
