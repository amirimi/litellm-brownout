from __future__ import annotations

from typing import Any

import yaml

from budget_policy.catalog import (
    PriceCatalog,
)
from budget_policy.manifest import (
    Manifest,
)

from .support import (
    CONFIG_DIR,
)


def test_the_catalog_covers_every_route_the_gateway_serves(catalog: PriceCatalog):
    with open(CONFIG_DIR / "config.yaml", encoding="utf-8") as handle:
        gateway_config: dict[str, Any] = yaml.safe_load(handle)

    for deployment in gateway_config["model_list"]:
        name = deployment["model_name"]
        price = catalog.price(name)
        assert price.data_class == deployment["model_info"]["data_class"]
        assert (
            price.input_cost_per_token
            == deployment["litellm_params"]["input_cost_per_token"]
        )
        assert (
            price.output_cost_per_token
            == deployment["litellm_params"]["output_cost_per_token"]
        )


def test_native_budget_alerting_is_not_a_second_tier_event_owner():
    with open(CONFIG_DIR / "config.yaml", encoding="utf-8") as handle:
        gateway_config: dict[str, Any] = yaml.safe_load(handle)

    assert "alerting" not in gateway_config["general_settings"]


def test_the_gateway_refuses_a_worst_case_that_does_not_fit():
    with open(CONFIG_DIR / "config.yaml", encoding="utf-8") as handle:
        gateway_config: dict[str, Any] = yaml.safe_load(handle)

    general_settings = gateway_config["general_settings"]
    assert general_settings.get("fail_closed_budget_enforcement") is True, (
        "without fail_closed_budget_enforcement the last hold shrinks to the headroom "
        "and a window can close one request above its envelope"
    )
    assert "disable_budget_reservation" not in general_settings, (
        "disable_budget_reservation turns admission into a plain counter read that "
        "concurrent requests can all pass on the same figure"
    )


def test_every_deployment_declares_its_output_ceiling():
    with open(CONFIG_DIR / "config.yaml", encoding="utf-8") as handle:
        gateway_config: dict[str, Any] = yaml.safe_load(handle)

    for deployment in gateway_config["model_list"]:
        ceiling = deployment["model_info"].get("max_output_tokens")
        assert isinstance(ceiling, int) and ceiling > 0, (
            f"deployment {deployment['model_name']} declares no max_output_tokens, so "
            "LiteLLM reserves 16384 output tokens for every request on it"
        )


def test_every_manifest_route_and_fallback_is_priced(
    manifest: Manifest, catalog: PriceCatalog
):
    for team_id in manifest.team_ids:
        policy = manifest.team_policy(team_id)
        for route, route_policy in policy.routes.items():
            catalog.price(route)
            if route_policy.fallback is not None:
                catalog.require_fallback(route, route_policy.fallback)


def test_every_manifest_route_and_fallback_exists_as_a_gateway_model_group(
    manifest: Manifest,
):
    with open(CONFIG_DIR / "config.yaml", encoding="utf-8") as handle:
        gateway_config: dict[str, Any] = yaml.safe_load(handle)
    served = {deployment["model_name"] for deployment in gateway_config["model_list"]}

    for team_id in manifest.team_ids:
        policy = manifest.team_policy(team_id)
        for route, route_policy in policy.routes.items():
            assert route in served, f"team {team_id} declares unserved route {route}"
            if route_policy.fallback is not None:
                assert route_policy.fallback in served, (
                    f"team {team_id} route {route} degrades to unserved "
                    f"{route_policy.fallback}"
                )


def test_the_gateway_declares_no_cross_group_router_fallbacks():
    with open(CONFIG_DIR / "config.yaml", encoding="utf-8") as handle:
        gateway_config: dict[str, Any] = yaml.safe_load(handle)

    router_settings = gateway_config.get("router_settings") or {}
    for key in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks"):
        assert key not in router_settings, (
            f"router_settings declares {key}, which can serve a governed request "
            "from a model group the disclosure header does not name"
        )
