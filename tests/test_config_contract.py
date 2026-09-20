"""Cross-file invariants among the manifest, the catalog and the gateway config.

These are the checks a merge gate runs. They are what makes an unpriced route or
a route the gateway cannot serve a review failure rather than a runtime one.
"""

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
    """The durable fix for the unpriced-model hole is a merge-time check.

    Every model group in config.yaml must carry a committed price and the same
    data class the gateway advertises for it.
    """
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
    """LiteLLM's hardcoded 85/95 alerter is off, so this component owns 75/90."""
    with open(CONFIG_DIR / "config.yaml", encoding="utf-8") as handle:
        gateway_config: dict[str, Any] = yaml.safe_load(handle)

    assert "alerting" not in gateway_config["general_settings"]


def test_the_gateway_refuses_a_worst_case_that_does_not_fit():
    """The hard boundary is one gateway flag, and the plugin never reads it.

    ``fail_closed_budget_enforcement`` makes LiteLLM refuse a worst case larger
    than the remaining headroom instead of shrinking the hold to fit
    (``budget_reservation.py:141``), and ``disable_budget_reservation`` would
    remove the hold from every budget on the gateway
    (``user_api_key_auth.py:2708``). Both are read from ``general_settings``
    alone, after the plugin has already loaded, so review time is the only place
    a drift can be caught.
    """
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
    """The worst-case hold reserves ``min(max_tokens, max_output_tokens or 16384)``.

    The router fills ``max_output_tokens`` from LiteLLM's cost map for a model the
    map knows (``router.py:10528-10535``). The lab's mock models are unknown to
    it, so a deployment that omits the field reserves 16384 output tokens per
    request (``budget_reservation.py:1434``), which is most of a lab envelope.
    """
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
    """A declared degradation target that is not priced would be rejected at runtime."""
    for team_id in manifest.team_ids:
        policy = manifest.team_policy(team_id)
        for route, route_policy in policy.routes.items():
            catalog.price(route)
            if route_policy.fallback is not None:
                assert (
                    catalog.select_fallback(route, route_policy.fallback)
                    == route_policy.fallback
                )


def test_every_manifest_route_and_fallback_exists_as_a_gateway_model_group(
    manifest: Manifest,
):
    """The inverse of the catalog check, and the direction that actually fails.

    A priced route the gateway does not serve passes every price check and then
    fails during routing, after the policy has already selected it. A degradation
    target is the dangerous case, because the caller never named it.
    """
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
    """``x-platform-route-served`` is recorded before routing, so it is only true
    while native failover stays among deployments of one model group.

    A ``fallbacks`` entry in ``router_settings`` would move a request to another
    model group after the header was decided, and the caller would be told it was
    served by a route that never ran.
    """
    with open(CONFIG_DIR / "config.yaml", encoding="utf-8") as handle:
        gateway_config: dict[str, Any] = yaml.safe_load(handle)

    router_settings = gateway_config.get("router_settings") or {}
    for key in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks"):
        assert key not in router_settings, (
            f"router_settings declares {key}, which can serve a governed request "
            "from a model group the disclosure header does not name"
        )
