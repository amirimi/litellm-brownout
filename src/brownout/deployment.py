from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final, assert_never

import yaml

from .compose_override import MANIFEST_MOUNT, PLUGIN_MOUNT
from .onboarding import AppendSetting, ConfigChange, RemoveSetting, SetSetting
from .plugin_folder import DISTRIBUTION

HELM_PLUGIN_VOLUME: Final = "brownout-plugin"
HELM_MANIFEST_VOLUME: Final = "brownout-manifest"
HELM_MANIFEST_DIRECTORY: Final = "/etc/brownout"
HELM_INSTALL_DIRECTORY: Final = "/plugins"
HELM_INSTALLER_IMAGE: Final = "python:3.13-slim"


def docker_run_flags(plugin_dir: Path, manifest: Path) -> str:
    return (
        f"-e PYTHONPATH={PLUGIN_MOUNT} "
        f"-v {plugin_dir.resolve()}:{PLUGIN_MOUNT}:ro "
        f"-v {manifest.resolve()}:{MANIFEST_MOUNT}:ro"
    )


def proxy_config_settings(
    changes: Iterable[ConfigChange], written: Mapping[str, Any]
) -> tuple[str, tuple[str, ...]]:
    settings: dict[str, dict[str, Any]] = {}
    removals: list[str] = []
    for change in changes:
        match change:
            case (
                SetSetting(section=section, key=key)
                | AppendSetting(section=section, key=key)
            ):
                settings.setdefault(section, {})[key] = written[section][key]
            case RemoveSetting(section=section, key=key):
                removals.append(f"{section}.{key}")
            case _:
                assert_never(change)
    if not settings:
        return "", tuple(removals)
    return _dump(settings), tuple(removals)


def helm_values(version: str, manifest_name: str) -> str:
    return _dump(
        {
            "extraInitContainers": [
                {
                    "name": HELM_PLUGIN_VOLUME,
                    "image": HELM_INSTALLER_IMAGE,
                    "command": [
                        "pip",
                        "install",
                        "--no-deps",
                        "--target",
                        HELM_INSTALL_DIRECTORY,
                        f"{DISTRIBUTION}=={version}",
                    ],
                    "volumeMounts": [
                        {
                            "name": HELM_PLUGIN_VOLUME,
                            "mountPath": HELM_INSTALL_DIRECTORY,
                        }
                    ],
                }
            ],
            "volumes": [
                {"name": HELM_PLUGIN_VOLUME, "emptyDir": {}},
                {
                    "name": HELM_MANIFEST_VOLUME,
                    "configMap": {"name": HELM_MANIFEST_VOLUME},
                },
            ],
            "volumeMounts": [
                {
                    "name": HELM_PLUGIN_VOLUME,
                    "mountPath": PLUGIN_MOUNT,
                    "readOnly": True,
                },
                {
                    "name": HELM_MANIFEST_VOLUME,
                    "mountPath": HELM_MANIFEST_DIRECTORY,
                    "readOnly": True,
                },
            ],
            "envVars": {
                "PYTHONPATH": PLUGIN_MOUNT,
                "BROWNOUT_MANIFEST": f"{HELM_MANIFEST_DIRECTORY}/{manifest_name}",
            },
        }
    )


def helm_manifest_command(manifest: Path) -> str:
    return f"kubectl create configmap {HELM_MANIFEST_VOLUME} --from-file={manifest}"


def _dump(document: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(document), sort_keys=False, default_flow_style=False)
