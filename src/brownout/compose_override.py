from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

PLUGIN_MOUNT: Final = "/app/plugins"
MANIFEST_MOUNT: Final = "/app/brownout.yaml"
PYTHONPATH: Final = "PYTHONPATH"
CONTAINER_PATH_SEPARATOR: Final = ":"
DEFAULT_BASE_FILES: Final = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)
LITELLM_REPOSITORIES: Final = (
    "docker.litellm.ai/berriai/litellm",
    "ghcr.io/berriai/litellm",
    "docker.io/litellm/litellm",
    "litellm/litellm",
)
OVERRIDE_INFIX: Final = ".override"

ComposeRunner = Callable[[Sequence[str], Path], tuple[int, str]]


@dataclass(frozen=True)
class ComposeService:
    name: str
    image: str | None
    environment: Mapping[str, str | None]
    mount_targets: frozenset[str]


@dataclass(frozen=True)
class ComposeProject:
    base_file: Path
    services: tuple[ComposeService, ...]


@dataclass(frozen=True)
class ComposeUnavailable:
    reason: str
    base_file: Path


@dataclass(frozen=True)
class NoComposeFile:
    directory: Path


@dataclass(frozen=True)
class Override:
    path: Path
    service: str
    text: str
    load_flags: str | None


@dataclass(frozen=True)
class OverrideRefused:
    reason: str
    text: str | None


def run_compose(arguments: Sequence[str], directory: Path) -> tuple[int, str]:
    result: Final = subprocess.run(
        list(arguments), capture_output=True, text=True, check=False, cwd=directory
    )
    return result.returncode, result.stdout


def base_compose_file(directory: Path) -> Path | None:
    for name in DEFAULT_BASE_FILES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def read_compose_project(
    base_file: Path, runner: ComposeRunner = run_compose
) -> ComposeProject | ComposeUnavailable:
    arguments: Final = [
        "docker",
        "compose",
        "-f",
        base_file.name,
        "config",
        "--format",
        "json",
    ]
    try:
        returncode, output = runner(arguments, base_file.parent)
    except FileNotFoundError:
        return ComposeUnavailable("docker is not installed on this machine", base_file)
    if returncode != 0:
        return ComposeUnavailable(
            f"docker compose config exited {returncode}", base_file
        )
    services: Final = json.loads(output).get("services") or {}
    return ComposeProject(
        base_file=base_file,
        services=tuple(_service(name, entry) for name, entry in services.items()),
    )


def override_text(service: str, pythonpath: str, plugin_dir: str, manifest: str) -> str:
    return yaml.safe_dump(
        {
            "services": {
                service: {
                    "environment": {PYTHONPATH: pythonpath},
                    "volumes": [
                        f"{plugin_dir}:{PLUGIN_MOUNT}:ro",
                        f"{manifest}:{MANIFEST_MOUNT}:ro",
                    ],
                }
            }
        },
        sort_keys=False,
        default_flow_style=False,
    )


def plan_override(
    project: ComposeProject,
    service_name: str | None,
    plugin_dir: Path,
    manifest: Path,
) -> Override | OverrideRefused:
    chosen: Final = _chosen_service(project, service_name)
    if isinstance(chosen, OverrideRefused):
        return chosen
    taken: Final = sorted(chosen.mount_targets & {PLUGIN_MOUNT, MANIFEST_MOUNT})
    if taken:
        return OverrideRefused(
            f"service {chosen.name!r} already mounts {', '.join(taken)}", None
        )
    directory: Final = project.base_file.parent
    text: Final = override_text(
        chosen.name,
        _pythonpath(chosen.environment),
        relative_to_project(plugin_dir, directory),
        relative_to_project(manifest, directory),
    )
    path: Final = override_path(project.base_file)
    if path.exists():
        return OverrideRefused(
            f"{path.name} already exists, so merge these lines into it by hand", text
        )
    return Override(
        path=path,
        service=chosen.name,
        text=text,
        load_flags=_load_flags(project.base_file, path),
    )


def _service(name: str, entry: Mapping[str, Any]) -> ComposeService:
    environment: Final = entry.get("environment") or {}
    volumes: Final = entry.get("volumes") or []
    return ComposeService(
        name=name,
        image=entry.get("image"),
        environment={str(key): value for key, value in environment.items()},
        mount_targets=frozenset(
            str(volume["target"]) for volume in volumes if "target" in volume
        ),
    )


def _chosen_service(
    project: ComposeProject, service_name: str | None
) -> ComposeService | OverrideRefused:
    if service_name is not None:
        for service in project.services:
            if service.name == service_name:
                return service
        return OverrideRefused(
            f"no service named {service_name!r}, and the project has "
            f"{_names(project.services)}",
            None,
        )
    running: Final = [
        service for service in project.services if _runs_litellm(service.image)
    ]
    if len(running) == 1:
        return running[0]
    if not running:
        return OverrideRefused(
            "no service runs LiteLLM's image, so name it with --service. The project "
            f"has {_names(project.services)}",
            None,
        )
    return OverrideRefused(
        f"several services run LiteLLM's image: {_names(running)}. Name one with "
        "--service",
        None,
    )


def _runs_litellm(image: str | None) -> bool:
    if image is None:
        return False
    repository: Final = _repository(image)
    return any(
        repository == known or repository.startswith(f"{known}-")
        for known in LITELLM_REPOSITORIES
    )


def _repository(image: str) -> str:
    without_digest: Final = image.split("@", 1)[0]
    name, _, tag = without_digest.rpartition(":")
    if name and "/" not in tag:
        return name
    return without_digest


def _names(services: Sequence[ComposeService]) -> str:
    return ", ".join(service.name for service in services)


def _pythonpath(environment: Mapping[str, str | None]) -> str:
    existing: Final = environment.get(PYTHONPATH)
    if not existing:
        return PLUGIN_MOUNT
    if PLUGIN_MOUNT in existing.split(CONTAINER_PATH_SEPARATOR):
        return existing
    return f"{existing}{CONTAINER_PATH_SEPARATOR}{PLUGIN_MOUNT}"


def relative_to_project(path: Path, directory: Path) -> str:
    relative: Final = os.path.relpath(path.resolve(), directory.resolve())
    if relative.startswith(os.pardir):
        return relative
    return f"./{relative}"


def override_path(base_file: Path) -> Path:
    return base_file.with_name(f"{base_file.stem}{OVERRIDE_INFIX}{base_file.suffix}")


def _load_flags(base_file: Path, override: Path) -> str | None:
    if base_file.name in DEFAULT_BASE_FILES:
        return None
    return f"-f {base_file.name} -f {override.name}"
