from __future__ import annotations

from typing import Any, Final

import litellm
import pytest

from brownout import bridge
from brownout.policy import Admit, Decision, Degraded, Served, WantsDegrade
from brownout.refusals import Refusal, RefusalReason, RuleId

from .support import make_auth

PRIMARY_GUARDRAIL: Final = "pii-policy"
FALLBACK_GUARDRAIL: Final = "residency-policy"


class FakeRouter:
    def __init__(self, groups: dict[str, list[str]]) -> None:
        self._groups: Final = groups

    def get_model_list(
        self, model_name: str, team_id: str | None = None
    ) -> list[dict[str, Any]]:
        guardrails = self._groups.get(model_name)
        if guardrails is None:
            return []
        return [
            {"litellm_params": {"model": model_name, "guardrails": list(guardrails)}}
        ]

    def get_deployment(self, model_id: str) -> Any:
        return None


@pytest.fixture
def guardrails() -> bridge.LiteLLMGuardrails:
    router = FakeRouter(
        {
            "budget-lab-primary": [PRIMARY_GUARDRAIL],
            "budget-lab-degraded": [FALLBACK_GUARDRAIL],
        }
    )
    return bridge.LiteLLMGuardrails(lambda: router)


def decision(
    wants: Admit | WantsDegrade, team_tiers: tuple[int, ...] = (90,)
) -> Decision:
    return Decision(
        route="budget-lab-primary",
        team_tiers=team_tiers,
        route_tiers=(),
        spend_usd=920.0,
        wants=wants,
    )


def test_a_rewrite_keeps_both_routes_guardrails(team, guardrails):
    data = {
        "model": "budget-lab-primary",
        "messages": [],
        "metadata": {"guardrails": [PRIMARY_GUARDRAIL]},
    }

    applied = bridge.apply(
        data,
        team,
        decision(WantsDegrade("budget-lab-degraded", 90)),
        Degraded("budget-lab-primary", "budget-lab-degraded", 90),
        guardrails,
    )

    assert applied["model"] == "budget-lab-degraded"
    assert set(applied["metadata"]["guardrails"]) == {
        PRIMARY_GUARDRAIL,
        FALLBACK_GUARDRAIL,
    }


def test_remerging_twice_changes_nothing(guardrails):
    data = {
        "model": "budget-lab-degraded",
        "messages": [],
        "metadata": {"guardrails": [PRIMARY_GUARDRAIL]},
    }

    once = guardrails.remerge(data)
    twice = guardrails.remerge(once)

    assert set(once["metadata"]["guardrails"]) == set(twice["metadata"]["guardrails"])


def test_an_admitted_request_is_not_rewritten(team, guardrails):
    data = {
        "model": "budget-lab-primary",
        "messages": [],
        "metadata": {"guardrails": [PRIMARY_GUARDRAIL]},
    }

    applied = bridge.apply(
        data, team, decision(Admit()), Served("budget-lab-primary"), guardrails
    )

    assert applied["model"] == "budget-lab-primary"
    assert applied["metadata"]["guardrails"] == [PRIMARY_GUARDRAIL]


def test_only_the_team_counter_hold_is_removed(team):
    caller = bridge.parse_caller(
        make_auth(team, reserved_cost=50.0, applied_adjustment=-10.0)
    )

    assert caller.own_hold_usd == 40.0


def test_a_caller_carries_its_key_budget_and_resolved_models(team):
    auth = make_auth(team)
    auth.max_budget = 5
    auth.team_models = ["budget-lab-primary"]
    auth.models = ["all-team-models"]

    caller = bridge.parse_caller(auth)

    assert caller.key_budgets == ("max_budget",)
    assert caller.key_models == ("budget-lab-primary",)
    assert caller.own_hold_usd == 0.0


def test_headers_carry_only_what_the_decision_has_to_say():
    data = {
        "metadata": {
            "_brownout": {
                "served_route": "chat-cheap",
                "degraded_from": "chat",
                "budget_tier": 90,
                "route_tier": None,
                "reason": "degrade_tier_reached",
            }
        }
    }

    assert bridge.headers(data, {"model_id": "dep-1"}) == {
        "x-brownout-route-served": "chat-cheap",
        "x-brownout-deployment-id": "dep-1",
        "x-brownout-budget-tier": "90",
        "x-brownout-degraded-from": "chat",
        "x-brownout-degradation-reason": "degrade_tier_reached",
    }
    assert bridge.headers({"metadata": {}}, None) is None


def test_caller_routing_keys_are_named():
    data = {
        "fallbacks": ["x"],
        "metadata": {"X-Brownout-Anything": 1, "degraded_from": "y", "ordinary": 2},
    }

    assert bridge.caller_routing_keys(data) == [
        "fallbacks",
        "metadata.X-Brownout-Anything",
        "metadata.degraded_from",
    ]


@pytest.mark.parametrize(
    ("reason", "status_code", "error"),
    [
        (
            RefusalReason.CALLER_ROUTING_NOT_PERMITTED,
            400,
            "caller_routing_not_permitted",
        ),
        (RefusalReason.UNPRICED_MODEL, 503, "brownout_unavailable"),
    ],
)
def test_a_refusal_becomes_one_http_error(reason, status_code, error):
    refusal = Refusal(
        rule=RuleId.MODEL_PRICED,
        reason=reason,
        message="m",
        detail={"model": "x"},
        incident_subject=None,
    )

    raised = bridge.http_error(refusal)

    assert raised.status_code == status_code
    assert raised.detail == {
        "error": error,
        "reason": reason.value,
        "rule": "model-priced",
        "model": "x",
        "message": "m",
    }


SHRINK_WARNING: Final = "a budget window can close above the envelope"


def test_a_release_below_the_floor_refuses_to_start():
    with pytest.raises(bridge.UnsupportedLiteLLM, match="1.95.1 and newer"):
        bridge.require_supported_litellm("1.95.0")


@pytest.mark.parametrize("release", ["1.101.0", "1.103.4", "1.104.2"])
def test_a_tested_release_that_refuses_whole_runs_quietly(release, caplog):
    bridge.require_supported_litellm(release)

    assert not [record for record in caplog.records if record.levelname == "WARNING"]


@pytest.mark.parametrize("release", ["1.95.1", "1.98.1", "1.100.5"])
def test_a_release_that_shrinks_holds_to_fit_runs_with_a_warning(release, caplog):
    bridge.require_supported_litellm(release)

    assert SHRINK_WARNING in caplog.text
    assert release in caplog.text


@pytest.mark.parametrize(
    ("release", "boundary"),
    [
        ("1.95.1", "shrink_to_fit"),
        ("1.100.9", "shrink_to_fit"),
        ("1.101.0", "refuse_whole"),
        ("1.104.2", "refuse_whole"),
        ("1.105.0rc1", "refuse_whole"),
    ],
)
def test_the_boundary_follows_the_release(release, boundary):
    assert bridge.boundary_for(release) == boundary


def test_a_release_past_the_ceiling_runs_with_a_warning(caplog):
    bridge.require_supported_litellm("1.105.0rc1")

    assert "newer than the newest release brownout was tested on" in caplog.text


def test_an_unreadable_release_refuses_to_start():
    with pytest.raises(bridge.UnsupportedLiteLLM, match="cannot read"):
        bridge.require_supported_litellm("main")


def test_the_live_gateway_state_reads_prices_classes_and_settings():
    router = litellm.Router(
        model_list=[
            {
                "model_name": "state-primary",
                "litellm_params": {
                    "model": "openai/state-primary",
                    "api_key": "sk-state",
                    "input_cost_per_token": 0.0001,
                    "output_cost_per_token": 0.001,
                },
                "model_info": {"data_class": "internal"},
            }
        ],
        fallbacks=[{"state-primary": ["elsewhere"]}],
    )
    state = bridge.LiteLLMGatewayState(
        lambda: router, lambda: {"fail_closed_budget_enforcement": True}
    )

    group = state.model_group("state-primary")
    assert group is not None
    assert (group.input_cost_per_token, group.output_cost_per_token) == (0.0001, 0.001)
    assert group.data_classes == ("internal",)
    assert state.model_group("never-configured") is None
    assert state.serves("state-primary", "team-a")
    settings = state.settings()
    assert settings.fail_closed_budget_enforcement is True
    assert settings.disable_budget_reservation is False
    assert settings.router_fallback_keys == ("fallbacks",)


def test_the_gateway_state_without_a_router_serves_nothing():
    state = bridge.LiteLLMGatewayState(lambda: None, dict)

    assert state.model_group("anything") is None
    assert state.serves("anything", "team-a") is False
    assert state.settings().router_fallback_keys == ()
