"""Guards for the etcd service definition shared by both compose layouts.

etcd is the only store of every APISIX route, upstream and consumer, and it was
migrated off the frozen ``bitnamilegacy/etcd:3.5.11`` image onto the upstream
CoreOS one. That image is distroless and does no auth bootstrap of its own, so
the migration rests on three things that are easy to break by accident and
invisible until a deploy: the data directory still being the old Bitnami path
(otherwise an existing volume silently comes up empty), the healthcheck being
exec-form (there is no shell to run a CMD-SHELL test with), and APISIX waiting
for the ``etcd-init`` bootstrap instead of only for etcd's health.

The two compose files are maintained as copies of each other, so every check
here runs against both.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
BLUEGREEN_INFRA_COMPOSE_FILE = REPO_ROOT / "docker-compose.infra.yml"
COMPOSE_FILES = (COMPOSE_FILE, BLUEGREEN_INFRA_COMPOSE_FILE)

ETCD_INIT_SCRIPT = REPO_ROOT / "etcd" / "init-auth.sh"
ETCD_DOCKERFILE = REPO_ROOT / "etcd" / "Dockerfile"
BACKUP_ETCD_LIB = REPO_ROOT / "backup" / "lib" / "etcd.sh"

# Upstream etcd, same 3.5 minor as the Bitnami image whose volumes must migrate
# in place. A jump to 3.6 is not a drop-in and must not slip in unnoticed.
ETCD_IMAGE_PATTERN = re.compile(r"^quay\.io/coreos/etcd:v3\.5\.\d+$")
ETCD_TOOLS_IMAGE_PATTERN = re.compile(r"^unibridge-etcd-tools:3\.5\.\d+$")

ETCD_DATA_MOUNT = "/bitnami/etcd"
ETCD_DATA_DIR = f"{ETCD_DATA_MOUNT}/data"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _service(path: Path, name: str) -> dict:
    services = _load_yaml(path)["services"]
    assert name in services, f"{path.name} has no '{name}' service"
    return services[name]


def _environment(service: dict) -> dict[str, str]:
    """Compose's list form (``- KEY=value``) as a dict."""
    env = service.get("environment", [])
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    parsed: dict[str, str] = {}
    for entry in env:
        key, _, value = str(entry).partition("=")
        parsed[key] = value
    return parsed


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=lambda p: p.name)
def test_etcd_runs_a_pinned_upstream_image(compose_path: Path) -> None:
    image = _service(compose_path, "etcd")["image"]

    assert "bitnami" not in image.lower(), (
        f"{compose_path.name} still runs a Bitnami etcd image ({image}); the "
        "bitnamilegacy archive is frozen and gets no security updates"
    )
    assert ETCD_IMAGE_PATTERN.match(image), (
        f"{compose_path.name} etcd image {image!r} does not match "
        f"{ETCD_IMAGE_PATTERN.pattern}"
    )


def test_both_compose_files_pin_the_same_etcd_image() -> None:
    images = {
        path.name: _service(path, "etcd")["image"] for path in COMPOSE_FILES
    }

    assert len(set(images.values())) == 1, (
        "the single-stack and blue-green infra stacks must run the same etcd "
        f"build, got {images}"
    )


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=lambda p: p.name)
def test_etcd_healthcheck_is_exec_form_etcdctl(compose_path: Path) -> None:
    test = _service(compose_path, "etcd")["healthcheck"]["test"]

    assert isinstance(test, list), (
        f"{compose_path.name} etcd healthcheck must be a list; the upstream "
        "image is distroless, so a string/CMD-SHELL test has no shell to run in"
    )
    assert test[0] == "CMD", (
        f"{compose_path.name} etcd healthcheck must use exec-form CMD, got {test[0]!r}"
    )
    assert test[1] == "etcdctl", (
        f"{compose_path.name} etcd healthcheck must invoke etcdctl, got {test[1]!r}"
    )


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=lambda p: p.name)
def test_etcd_keeps_the_legacy_data_path_so_volumes_migrate_in_place(
    compose_path: Path,
) -> None:
    service = _service(compose_path, "etcd")
    env = _environment(service)

    assert env.get("ETCD_DATA_DIR") == ETCD_DATA_DIR, (
        f"{compose_path.name} must keep ETCD_DATA_DIR={ETCD_DATA_DIR}: the "
        "upstream image defaults elsewhere, and an existing etcd-data volume "
        "written by the Bitnami image would come up empty — every APISIX "
        "route, upstream and consumer silently gone"
    )
    mounts = [str(v).split(":")[1] for v in service["volumes"] if ":" in str(v)]
    assert ETCD_DATA_MOUNT in mounts, (
        f"{compose_path.name} must mount the etcd volume at {ETCD_DATA_MOUNT}, got {mounts}"
    )


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=lambda p: p.name)
def test_etcd_healthcheck_can_authenticate(compose_path: Path) -> None:
    env = _environment(_service(compose_path, "etcd"))

    assert env.get("ETCDCTL_USER") == "root:${ETCD_ROOT_PASSWORD:-}", (
        f"{compose_path.name} etcd needs ETCDCTL_USER for its healthcheck: "
        "`etcdctl endpoint health` is rejected once auth is enabled unless it "
        "authenticates, so without it the container never reports healthy"
    )


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=lambda p: p.name)
def test_etcd_init_bootstraps_auth_after_etcd_is_healthy(compose_path: Path) -> None:
    service = _service(compose_path, "etcd-init")

    assert ETCD_TOOLS_IMAGE_PATTERN.match(service["image"]), (
        f"{compose_path.name} etcd-init image {service['image']!r} does not match "
        f"{ETCD_TOOLS_IMAGE_PATTERN.pattern}"
    )
    assert service.get("build") == "./etcd"
    assert service.get("restart") == "no", (
        f"{compose_path.name} etcd-init is a one-shot bootstrap: any restart "
        "policy other than \"no\" turns a refusal into a crash loop"
    )
    assert service["depends_on"]["etcd"]["condition"] == "service_healthy"

    env = _environment(service)
    assert env.get("ETCD_ROOT_PASSWORD") == "${ETCD_ROOT_PASSWORD:-}"
    assert env.get("ETCD_ALLOW_NONE_AUTH") == "${ETCD_ALLOW_NONE_AUTH:-no}"


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=lambda p: p.name)
def test_apisix_waits_for_the_auth_bootstrap(compose_path: Path) -> None:
    depends_on = _service(compose_path, "apisix")["depends_on"]

    assert depends_on["etcd"]["condition"] == "service_healthy"
    assert depends_on["etcd-init"]["condition"] == "service_completed_successfully", (
        f"{compose_path.name} apisix must wait for etcd-init: starting against "
        "an etcd whose auth bootstrap has not finished connects fine and then "
        "breaks the moment auth is switched on"
    )


def test_etcd_init_script_is_executable_posix_sh() -> None:
    assert ETCD_INIT_SCRIPT.exists(), f"{ETCD_INIT_SCRIPT} is missing"
    assert ETCD_INIT_SCRIPT.stat().st_mode & 0o111, (
        f"{ETCD_INIT_SCRIPT} must be executable: it is the image ENTRYPOINT"
    )

    result = subprocess.run(
        ["sh", "-n", str(ETCD_INIT_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"sh -n failed:\n{result.stderr}"


def test_etcd_dockerfile_builds_the_tools_image_from_the_pinned_etcd() -> None:
    dockerfile = ETCD_DOCKERFILE.read_text(encoding="utf-8")

    match = re.search(r"^ARG ETCD_IMAGE=(\S+)", dockerfile, re.MULTILINE)
    assert match, "etcd/Dockerfile must declare ARG ETCD_IMAGE"
    assert ETCD_IMAGE_PATTERN.match(match.group(1)), (
        f"etcd/Dockerfile base image {match.group(1)!r} does not match "
        f"{ETCD_IMAGE_PATTERN.pattern}"
    )
    # The compose etcd image and the tools image must come from one etcd build,
    # or etcdctl/etcdutl can drift away from the server they talk to.
    assert match.group(1) == _service(COMPOSE_FILE, "etcd")["image"]

    for binary in ("etcdctl", "etcdutl"):
        assert f"/usr/local/bin/{binary}" in dockerfile, (
            f"etcd/Dockerfile must copy {binary} out of the distroless etcd image"
        )


def test_backup_lib_no_longer_reaches_for_the_bitnami_image() -> None:
    lib = BACKUP_ETCD_LIB.read_text(encoding="utf-8")

    # The data path stays "/bitnami/etcd" on purpose so existing volumes
    # migrate in place, so only that spelling is allowed to survive here.
    stray = [
        line.strip()
        for line in lib.splitlines()
        if "bitnami" in line.lower() and ETCD_DATA_MOUNT not in line
    ]
    assert stray == [], (
        "backup/lib/etcd.sh still references the Bitnami etcd image: " f"{stray}"
    )

    assert "ETCD_TOOLS_IMAGE" in lib, (
        "backup/lib/etcd.sh must run snapshot save/restore from the shell-capable "
        "tools image; the etcd image itself has no shell, no rm and no etcdutl entrypoint"
    )
    code = [
        line for line in lib.splitlines() if not line.lstrip().startswith("#")
    ]
    assert not [line for line in code if "chown" in line], (
        "the upstream etcd image runs as root, so the post-restore chown to uid "
        "1001 the Bitnami image needed would now hand etcd a data dir it cannot write"
    )
