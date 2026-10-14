from __future__ import annotations

from dataclasses import replace

import pytest

from brownout.manifest import Manifest, RoutePolicy
from brownout.refusals import RuleId
from brownout.rules import (
    GatewayFile,
    GatewaySettings,
    ModelGroup,
    file_rules,
    is_priced,
    route_rules,
    settings_rules,
)

from .support import DATA_CLASS_ORDER, gateway_file_of, lab_gateway_config

PAIR = RoutePolicy(route="primary", fallback="cheaper", degrade_at=90, warn_tiers=())
SAFE_SETTINGS = GatewaySettings(
    fail_closed_budget_enforcement=True,
    disable_budget_reservation=False,
)


def group(
    name: str,
    data_classes: tuple[str | None, ...] = ("customer_pii",),
    input_cost: float | None = 0.001,
    output_cost: float | None = 0.002,
) -> ModelGroup:
    return ModelGroup(
        name=name,
        input_cost_per_token=input_cost,
        output_cost_per_token=output_cost,
        data_classes=data_classes,
    )


def manifest() -> Manifest:
    return Manifest(version="test", data_class_order=DATA_CLASS_ORDER, teams={})


def rules_for(*groups: ModelGroup) -> list[RuleId]:
    lookup = {item.name: item for item in groups}
    return [violation.rule for violation in route_rules(manifest(), PAIR, lookup.get)]


def test_the_lab_config_and_manifest_pass_every_rule(manifest):
    assert file_rules(manifest, gateway_file_of(lab_gateway_config())) == []


def test_a_served_priced_classified_pair_passes():
    assert rules_for(group("primary"), group("cheaper")) == []


def test_a_dearer_fallback_is_the_route_owners_call():
    dearer = group("cheaper", input_cost=0.01, output_cost=0.02)

    assert rules_for(group("primary"), dearer) == []


def test_an_unserved_fallback_is_a_violation():
    assert rules_for(group("primary")) == [RuleId.ROUTE_SERVED]


def test_an_unpriced_fallback_is_a_violation():
    assert rules_for(group("primary"), group("cheaper", output_cost=None)) == [
        RuleId.ROUTE_PRICED
    ]


def test_deployments_that_disagree_on_a_data_class_are_a_violation():
    split = group("cheaper", data_classes=("customer_pii", "internal"))

    assert rules_for(group("primary"), split) == [RuleId.DATA_CLASS_DECLARED]


@pytest.mark.parametrize(
    ("data_class", "message"),
    [
        pytest.param(
            None,
            "has no model_info.data_class. A route with a fallback and its fallback "
            "each need one from data_class_order ['internal', 'customer_pii']",
            id="missing",
        ),
        pytest.param(
            "secret",
            "declares data class 'secret', which is not in the manifest's "
            "data_class_order ['internal', 'customer_pii']",
            id="unlisted",
        ),
    ],
)
def test_a_data_class_outside_the_order_names_the_fix(data_class, message):
    lookup = {
        item.name: item
        for item in (group("primary"), group("cheaper", data_classes=(data_class,)))
    }

    violations = route_rules(manifest(), PAIR, lookup.get)

    assert [(violation.rule, violation.message) for violation in violations] == [
        (RuleId.DATA_CLASS_DECLARED, message)
    ]


def test_a_less_restricted_fallback_is_a_violation():
    assert rules_for(
        group("primary"), group("cheaper", data_classes=("internal",))
    ) == [RuleId.FALLBACK_DATA_CLASS]


def test_a_route_without_a_fallback_needs_only_to_be_served_and_priced():
    plain = RoutePolicy(route="primary", fallback=None, degrade_at=None, warn_tiers=())
    unclassified = group("primary", data_classes=(None,))

    assert route_rules(manifest(), plain, {"primary": unclassified}.get) == []


@pytest.mark.parametrize(
    ("settings", "rule"),
    [
        (
            replace(SAFE_SETTINGS, fail_closed_budget_enforcement=False),
            RuleId.FAIL_CLOSED_ON,
        ),
        (
            replace(SAFE_SETTINGS, disable_budget_reservation=True),
            RuleId.RESERVATION_ON,
        ),
    ],
)
def test_each_gateway_setting_has_its_rule(settings, rule):
    assert [violation.rule for violation in settings_rules(settings)] == [rule]


def test_safe_settings_pass():
    assert settings_rules(SAFE_SETTINGS) == []


def test_native_budget_alerts_and_a_missing_redis_are_file_violations():
    gateway = GatewayFile(
        groups={},
        settings=SAFE_SETTINGS,
        native_budget_alerts=True,
        redis_configured=False,
    )

    violations = file_rules(manifest(), gateway)

    assert [violation.rule for violation in violations] == [
        RuleId.NO_NATIVE_BUDGET_ALERTS,
        RuleId.REDIS_CONFIGURED,
    ]
    assert "Redis cache" in violations[1].message


@pytest.mark.parametrize(
    ("input_cost", "output_cost", "priced"),
    [
        (0.001, 0.002, True),
        (0.0, 0.002, True),
        (None, 0.002, False),
        (0.0, 0.0, False),
        (float("nan"), 0.002, False),
        (-0.001, 0.002, False),
    ],
)
def test_what_counts_as_priced(input_cost, output_cost, priced):
    assert (
        is_priced(group("g", input_cost=input_cost, output_cost=output_cost)) is priced
    )
