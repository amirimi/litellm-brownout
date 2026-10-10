from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from importlib.metadata import version
from pathlib import Path
from typing import Any, Final

import litellm
import yaml

from .manifest import Manifest, ManifestError
from .refusals import Violation
from .rules import (
    ROUTER_FALLBACK_KEYS,
    GatewayFile,
    GatewaySettings,
    ModelGroup,
    file_rules,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser: Final = argparse.ArgumentParser(prog="brownout")
    commands: Final = parser.add_subparsers(dest="command", required=True)
    check_command: Final = commands.add_parser(
        "check", help="Check a gateway config and a manifest against brownout's rules"
    )
    check_command.add_argument("--gateway-config", required=True, type=Path)
    check_command.add_argument("--manifest", required=True, type=Path)
    arguments: Final = parser.parse_args(argv)

    violations: Final = check(arguments.gateway_config, arguments.manifest)
    for violation in violations:
        print(f"{violation.rule}\t{violation.subject}\t{violation.message}")
    if violations:
        print(f"{len(violations)} rule violation(s)", file=sys.stderr)
        return 1
    print(f"brownout check passed on LiteLLM {litellm_release()}")
    return 0


def check(gateway_config: Path, manifest_path: Path) -> list[Violation]:
    try:
        manifest = Manifest.from_file(manifest_path)
    except ManifestError as error:
        return [Violation(error.rule, str(manifest_path), str(error))]
    return file_rules(manifest, gateway_file(_load_yaml(gateway_config)))


def gateway_file(config: Mapping[str, Any]) -> GatewayFile:
    general: Final = config.get("general_settings") or {}
    router_settings: Final = config.get("router_settings") or {}
    deployments_by_group: dict[str, list[Mapping[str, Any]]] = {}
    for deployment in config.get("model_list") or []:
        deployments_by_group.setdefault(str(deployment["model_name"]), []).append(
            deployment
        )
    return GatewayFile(
        groups={
            name: _model_group(name, deployments)
            for name, deployments in deployments_by_group.items()
        },
        settings=GatewaySettings(
            fail_closed_budget_enforcement=general.get("fail_closed_budget_enforcement")
            is True,
            disable_budget_reservation=general.get("disable_budget_reservation")
            is True,
            router_fallback_keys=tuple(
                key for key in ROUTER_FALLBACK_KEYS if router_settings.get(key)
            ),
        ),
        native_alerting=bool(general.get("alerting")),
        redis_configured=bool(general.get("coordination_redis")),
    )


def litellm_release() -> str:
    return version("litellm")


def _model_group(name: str, deployments: list[Mapping[str, Any]]) -> ModelGroup:
    prices = [_deployment_prices(deployment) for deployment in deployments]
    input_costs = [price[0] for price in prices]
    output_costs = [price[1] for price in prices]
    return ModelGroup(
        name=name,
        input_cost_per_token=_highest_known(input_costs),
        output_cost_per_token=_highest_known(output_costs),
        data_classes=tuple(
            (deployment.get("model_info") or {}).get("data_class")
            for deployment in deployments
        ),
    )


def _deployment_prices(
    deployment: Mapping[str, Any],
) -> tuple[float | None, float | None]:
    params: Final = deployment.get("litellm_params") or {}
    mapped: Final = _cost_map_entry(str(params.get("model") or ""))
    return (
        _price(params.get("input_cost_per_token"), mapped.get("input_cost_per_token")),
        _price(
            params.get("output_cost_per_token"), mapped.get("output_cost_per_token")
        ),
    )


def _cost_map_entry(model: str) -> Mapping[str, Any]:
    exact = litellm.model_cost.get(model)
    if exact is not None:
        return exact
    return litellm.model_cost.get(model.split("/", 1)[-1]) or {}


def _price(explicit: Any, mapped: Any) -> float | None:
    value = explicit
    if value is None:
        value = mapped
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _highest_known(values: list[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    if len(known) != len(values) or not known:
        return None
    return max(known)


def _load_yaml(path: Path) -> Mapping[str, Any]:
    with open(path, encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        return {}
    return document


if __name__ == "__main__":
    raise SystemExit(main())
