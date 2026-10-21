from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from brownout.compose_override import (
    ComposeUnavailable,
    Override,
    OverrideRefused,
    base_compose_file,
    plan_override,
    read_compose_project,
)

LITELLM_IMAGE: Final = "docker.litellm.ai/berriai/litellm:v1.106.2"
BASE_SERVICE: Final = {
    "image": LITELLM_IMAGE,
    "ports": ["4000:4000"],
    "environment": {"STORE_MODEL_IN_DB": "True"},
    "volumes": ["./config.yaml:/app/config.yaml:ro"],
    "command": ["--config", "/app/config.yaml", "--port", "4000"],
}


def effective(services: dict[str, dict[str, Any]]) -> str:
    resolved = {
        name: {
            "image": service.get("image"),
            "environment": service.get("environment", {}),
            "volumes": [
                {"type": "bind", "source": "/x", "target": target}
                for target in service.get("targets", [])
            ],
        }
        for name, service in services.items()
    }
    return json.dumps({"name": "stack", "services": resolved})


def fake_compose(output: str, returncode: int = 0):
    def run(arguments: Sequence[str], directory: Path) -> tuple[int, str]:
        assert list(arguments[:2]) == ["docker", "compose"]
        assert "config" in arguments
        return returncode, output

    return run


def project(
    tmp_path: Path,
    services: dict[str, dict[str, Any]],
    name: str = "docker-compose.yml",
):
    base = tmp_path / name
    base.write_text("services: {}\n", "utf-8")
    read = read_compose_project(base, fake_compose(effective(services)))
    assert not isinstance(read, ComposeUnavailable)
    return read


def plan(
    tmp_path: Path,
    services: dict[str, dict[str, Any]],
    service: str | None = None,
    name: str = "docker-compose.yml",
):
    return plan_override(
        project(tmp_path, services, name),
        service,
        tmp_path / "plugins",
        tmp_path / "brownout.yaml",
    )


def written(override: Override) -> dict[str, Any]:
    return yaml.safe_load(override.text)


def test_the_override_adds_the_plugin_to_the_one_litellm_service(tmp_path):
    override = plan(
        tmp_path, {"proxy": {"image": LITELLM_IMAGE}, "db": {"image": "postgres:16"}}
    )

    assert isinstance(override, Override)
    assert override.path == tmp_path / "docker-compose.override.yml"
    assert written(override) == {
        "services": {
            "proxy": {
                "environment": {"PYTHONPATH": "/app/plugins"},
                "volumes": [
                    "./plugins:/app/plugins:ro",
                    "./brownout.yaml:/app/brownout.yaml:ro",
                ],
            }
        }
    }
    assert override.load_flags is None


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/berriai/litellm:main-v1.106.2",
        "ghcr.io/berriai/litellm-database:main-v1.106.2",
        "litellm/litellm:v1.106.2",
        "docker.litellm.ai/berriai/litellm@sha256:" + "a" * 64,
    ],
)
def test_every_litellm_image_is_recognised(tmp_path, image):
    assert isinstance(plan(tmp_path, {"gateway": {"image": image}}), Override)


def test_a_named_service_is_used_even_when_it_builds_its_own_image(tmp_path):
    override = plan(
        tmp_path, {"gateway": {"image": "stack-gateway"}}, service="gateway"
    )

    assert isinstance(override, Override)
    assert "gateway" in written(override)["services"]


@pytest.mark.parametrize(
    ("services", "service", "reason"),
    [
        ({"db": {"image": "postgres:16"}}, None, "no service runs LiteLLM's image"),
        (
            {"a": {"image": LITELLM_IMAGE}, "b": {"image": LITELLM_IMAGE}},
            None,
            "several services run LiteLLM's image: a, b",
        ),
        ({"proxy": {"image": LITELLM_IMAGE}}, "gateway", "no service named 'gateway'"),
        (
            {"proxy": {"image": LITELLM_IMAGE, "targets": ["/app/plugins"]}},
            None,
            "already mounts /app/plugins",
        ),
        (
            {"proxy": {"image": LITELLM_IMAGE, "targets": ["/app/brownout.yaml"]}},
            None,
            "already mounts /app/brownout.yaml",
        ),
    ],
)
def test_a_service_brownout_cannot_extend_cleanly_is_refused(
    tmp_path, services, service, reason
):
    refused = plan(tmp_path, services, service)

    assert isinstance(refused, OverrideRefused)
    assert reason in refused.reason


def test_an_existing_override_file_is_left_alone(tmp_path):
    (tmp_path / "docker-compose.override.yml").write_text("services: {}\n", "utf-8")

    refused = plan(tmp_path, {"proxy": {"image": LITELLM_IMAGE}})

    assert isinstance(refused, OverrideRefused)
    assert "docker-compose.override.yml already exists" in refused.reason
    assert refused.text is not None and "/app/plugins" in refused.text


def test_a_pythonpath_the_service_already_sets_keeps_its_entries(tmp_path):
    override = plan(
        tmp_path,
        {
            "proxy": {
                "image": LITELLM_IMAGE,
                "environment": {"PYTHONPATH": "/opt/team-libs"},
            }
        },
    )

    assert isinstance(override, Override)
    environment = written(override)["services"]["proxy"]["environment"]
    assert environment == {"PYTHONPATH": "/opt/team-libs:/app/plugins"}


@pytest.mark.parametrize(
    ("name", "override_name"),
    [
        ("compose.yaml", "compose.override.yaml"),
        ("compose.yml", "compose.override.yml"),
        ("docker-compose.yaml", "docker-compose.override.yaml"),
    ],
)
def test_the_override_takes_the_name_compose_loads_for_the_base_file(
    tmp_path, name, override_name
):
    override = plan(tmp_path, {"proxy": {"image": LITELLM_IMAGE}}, name=name)

    assert isinstance(override, Override)
    assert override.path.name == override_name
    assert override.load_flags is None


def test_a_base_file_compose_does_not_load_by_default_gets_the_flags_to_add(tmp_path):
    override = plan(tmp_path, {"proxy": {"image": LITELLM_IMAGE}}, name="prod.yml")

    assert isinstance(override, Override)
    assert override.path.name == "prod.override.yml"
    assert override.load_flags == "-f prod.yml -f prod.override.yml"


def test_the_base_file_is_found_in_composes_order(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("", "utf-8")
    (tmp_path / "compose.yaml").write_text("", "utf-8")

    assert base_compose_file(tmp_path) == tmp_path / "compose.yaml"


def test_a_folder_without_a_compose_file_has_no_base_file(tmp_path):
    assert base_compose_file(tmp_path) is None


def test_a_missing_docker_makes_compose_unavailable(tmp_path):
    def missing(arguments: Sequence[str], directory: Path) -> tuple[int, str]:
        raise FileNotFoundError("docker")

    base = tmp_path / "docker-compose.yml"
    base.write_text("services: {}\n", "utf-8")

    assert isinstance(read_compose_project(base, missing), ComposeUnavailable)


def test_a_failing_compose_config_makes_compose_unavailable(tmp_path):
    base = tmp_path / "docker-compose.yml"
    base.write_text("services: {}\n", "utf-8")

    unavailable = read_compose_project(base, fake_compose("", returncode=15))

    assert isinstance(unavailable, ComposeUnavailable)
    assert "exited 15" in unavailable.reason


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker compose")
def test_compose_merges_the_override_into_a_renamed_service_with_its_own_pythonpath(
    tmp_path,
):
    (tmp_path / "team.env").write_text("PYTHONPATH=/opt/team-libs\n", "utf-8")
    (tmp_path / "docker-compose.yml").write_text(
        yaml.safe_dump(
            {
                "services": {
                    "gateway": {**BASE_SERVICE, "env_file": ["team.env"]},
                    "db": {"image": "postgres:16"},
                }
            }
        ),
        "utf-8",
    )
    read = read_compose_project(tmp_path / "docker-compose.yml")
    assert not isinstance(read, ComposeUnavailable)
    override = plan_override(
        read, "gateway", tmp_path / "plugins", tmp_path / "brownout.yaml"
    )
    assert isinstance(override, Override)
    override.path.write_text(override.text, "utf-8")

    merged = json.loads(
        subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            capture_output=True,
            text=True,
            check=True,
            cwd=tmp_path,
        ).stdout
    )["services"]["gateway"]

    assert merged["image"] == LITELLM_IMAGE
    assert merged["command"] == BASE_SERVICE["command"]
    assert merged["environment"]["PYTHONPATH"] == "/opt/team-libs:/app/plugins"
    assert merged["environment"]["STORE_MODEL_IN_DB"] == "True"
    assert {volume["target"] for volume in merged["volumes"]} == {
        "/app/config.yaml",
        "/app/plugins",
        "/app/brownout.yaml",
    }
