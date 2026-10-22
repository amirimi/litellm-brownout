from __future__ import annotations

from dataclasses import replace

import pytest

from brownout.manifest import Manifest, RoutePolicy
from brownout.refusals import RuleId
from brownout.rules import (
    GatewayFile,
    GatewaySettings,
    ModelGroup,
    file_notices,
    file_rules,
    group_from_deployments,
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
    return Manifest(data_class_order=DATA_CLASS_ORDER, teams={})


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


def test_an_unpriced_group_names_both_fixes():
    lookup = {
        item.name: item
        for item in (group("primary"), group("cheaper", output_cost=None))
    }

    violations = route_rules(manifest(), PAIR, lookup.get)

    assert "input_cost_per_token" in violations[0].message
    assert "model_info.base_model" in violations[0].message


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


def unordered() -> Manifest:
    return Manifest(data_class_order=(), teams={})


@pytest.mark.parametrize(
    "fallback",
    [
        pytest.param(group("cheaper", data_classes=(None,)), id="unclassified"),
        pytest.param(
            group("cheaper", data_classes=("internal",)), id="less-restricted"
        ),
        pytest.param(
            group("cheaper", data_classes=("customer_pii", "internal")), id="split"
        ),
    ],
)
def test_without_a_data_class_order_a_pairing_needs_only_to_be_served_and_priced(
    fallback,
):
    lookup = {item.name: item for item in (group("primary", (None,)), fallback)}

    assert route_rules(unordered(), PAIR, lookup.get) == []


def test_without_a_data_class_order_an_unpriced_fallback_is_still_a_violation():
    lookup = {
        item.name: item
        for item in (group("primary", (None,)), group("cheaper", (None,), None))
    }

    assert [
        violation.rule for violation in route_rules(unordered(), PAIR, lookup.get)
    ] == [RuleId.ROUTE_PRICED]


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


def test_a_missing_redis_is_a_notice_and_never_a_violation():
    gateway = GatewayFile(groups={}, settings=SAFE_SETTINGS, redis_configured=False)

    notices = file_notices(gateway)

    assert file_rules(manifest(), gateway) == []
    assert [notice.rule for notice in notices] == [RuleId.REDIS_CONFIGURED]
    assert "REDIS_*" in notices[0].message
    assert "one LiteLLM process" in notices[0].message


def test_a_configured_redis_raises_no_notice():
    gateway = GatewayFile(groups={}, settings=SAFE_SETTINGS, redis_configured=True)

    assert file_notices(gateway) == []


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


@pytest.mark.parametrize(
    "unpriced",
    [
        pytest.param((None, None), id="missing"),
        pytest.param((0.0, 0.0), id="zero-as-1.101.6-reports-it"),
        pytest.param((-1.0, 0.002), id="negative"),
    ],
)
def test_a_group_with_one_unpriced_deployment_is_unpriced(unpriced):
    group = group_from_deployments("mixed", [(0.001, 0.002), unpriced], ())

    assert not is_priced(group)


def test_a_group_takes_the_highest_price_of_its_priced_deployments():
    group = group_from_deployments("pair", [(0.001, 0.004), (0.003, 0.002)], ())

    assert (group.input_cost_per_token, group.output_cost_per_token) == (0.003, 0.004)
