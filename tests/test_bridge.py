from __future__ import annotations

from typing import Any, Final

import litellm
import pytest

from brownout import bridge
from brownout.policy import (
    Admit,
    Decision,
    Degraded,
    Rejected,
    Rejection,
    Served,
    WantsDegrade,
)
from brownout.refusals import Refusal, RefusalReason, RuleId
from brownout.rules import is_priced

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
        data, Degraded("budget-lab-primary", "budget-lab-degraded", 90), guardrails
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

    applied = bridge.apply(data, Served("budget-lab-primary"), guardrails)

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


SERVED_RESPONSE: Final = object()


def with_field(field: dict[str, Any]) -> dict[str, Any]:
    return {"metadata": {"spend_logs_metadata": {"brownout": field}}}


def test_headers_carry_only_what_the_decision_has_to_say():
    data = with_field(
        {
            "requested_route": "chat",
            "outcome": "degraded",
            "served_route": "chat-cheap",
            "budget_tier": "90",
            "degradation_reason": "degrade_tier_reached",
            "utilisation_percent": 91.0,
        }
    )

    assert bridge.headers(data, SERVED_RESPONSE, {"model_id": "dep-1"}) == {
        "x-brownout-route-served": "chat-cheap",
        "x-brownout-deployment-id": "dep-1",
        "x-brownout-budget-tier": "90",
        "x-brownout-degraded-from": "chat",
        "x-brownout-degradation-reason": "degrade_tier_reached",
    }
    assert bridge.headers({"metadata": {}}, SERVED_RESPONSE, None) is None


def test_a_failed_request_never_claims_a_route_served():
    data = with_field(
        {
            "requested_route": "chat",
            "outcome": "degraded",
            "served_route": "chat-cheap",
            "budget_tier": "90",
            "degradation_reason": "degrade_tier_reached",
        }
    )

    assert bridge.headers(data, None, {"model_id": "dep-1"}) == {
        "x-brownout-budget-tier": "90",
        "x-brownout-degraded-from": "chat",
        "x-brownout-degradation-reason": "degrade_tier_reached",
    }


def test_a_failed_request_with_nothing_to_disclose_carries_no_headers():
    data = with_field(
        {
            "requested_route": "chat",
            "outcome": "served",
            "served_route": "chat",
            "utilisation_percent": 10.0,
        }
    )

    assert bridge.headers(data, None, None) is None


def test_a_refused_request_carries_no_disclosure_headers():
    data = with_field(
        {"requested_route": "chat", "outcome": "refused", "refusal": "unpriced_model"}
    )

    assert bridge.headers(data, SERVED_RESPONSE, None) is None


def test_the_field_is_written_into_a_fresh_dict():
    shared = {"cost_center": "k-1"}
    data: dict[str, Any] = {"metadata": {"spend_logs_metadata": shared}}

    bridge.write_field(data, {"outcome": "served"})

    assert shared == {"cost_center": "k-1"}
    assert data["metadata"]["spend_logs_metadata"] == {
        "cost_center": "k-1",
        "brownout": {"outcome": "served"},
    }


@pytest.mark.parametrize("existing", ["a-string", ["a-list"], None])
def test_a_spend_logs_metadata_that_is_not_a_dict_is_replaced(existing):
    data: dict[str, Any] = {"metadata": {"spend_logs_metadata": existing}}

    bridge.write_field(data, {"outcome": "served"})

    assert data["metadata"]["spend_logs_metadata"] == {
        "brownout": {"outcome": "served"}
    }


def test_a_request_without_metadata_gains_the_field():
    data: dict[str, Any] = {}

    bridge.write_field(data, {"outcome": "served"})

    assert data == {
        "metadata": {"spend_logs_metadata": {"brownout": {"outcome": "served"}}}
    }


def test_a_forged_field_is_cleared_from_both_containers_without_mutation():
    shared = {"brownout": {"outcome": "forged"}, "cost_center": "k-1"}
    data: dict[str, Any] = {
        "metadata": {"spend_logs_metadata": shared},
        "litellm_metadata": {"spend_logs_metadata": {"brownout": {}}},
    }

    bridge.clear_field(data)

    assert shared == {"brownout": {"outcome": "forged"}, "cost_center": "k-1"}
    assert data["metadata"]["spend_logs_metadata"] == {"cost_center": "k-1"}
    assert data["litellm_metadata"]["spend_logs_metadata"] == {}


def test_clearing_leaves_a_request_without_the_field_untouched():
    spend_logs_metadata = {"cost_center": "k-1"}
    data: dict[str, Any] = {"metadata": {"spend_logs_metadata": spend_logs_metadata}}

    bridge.clear_field(data)

    assert data["metadata"]["spend_logs_metadata"] is spend_logs_metadata


def test_caller_routing_keys_are_named():
    data = {
        "fallbacks": ["x"],
        "context_window_fallbacks": ["y"],
        "content_policy_fallbacks": ["z"],
        "metadata": {"X-Brownout-Anything": 1, "degraded_from": "y", "ordinary": 2},
    }

    assert bridge.caller_routing_keys(data) == [
        "content_policy_fallbacks",
        "context_window_fallbacks",
        "fallbacks",
    ]


@pytest.mark.parametrize(
    "outcome",
    [
        Served("budget-lab-primary"),
        Degraded("budget-lab-primary", "budget-lab-degraded", 90),
        Rejected(
            "budget-lab-primary",
            "budget-lab-degraded",
            90,
            Rejection.OUTSIDE_CALLER_ALLOWLIST,
        ),
    ],
)
def test_every_governed_request_has_its_fallbacks_emptied(guardrails, outcome):
    team_settings = {"fallbacks": [{"budget-lab-primary": ["elsewhere"]}]}
    data = {
        "model": "budget-lab-primary",
        "messages": [],
        "metadata": {},
        "router_settings_override": team_settings,
    }

    applied = bridge.apply(data, outcome, guardrails)

    assert applied["fallbacks"] == []
    assert applied["context_window_fallbacks"] == []
    assert applied["content_policy_fallbacks"] == []
    assert applied["disable_fallbacks"] is True
    assert applied["router_settings_override"] == team_settings


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


def _deployment(
    name: str, model: str, prices: tuple[float, float] | None, info: dict | None = None
) -> dict:
    params: dict = {"model": model, "api_key": "sk-state"}
    if prices is not None:
        params["input_cost_per_token"] = prices[0]
        params["output_cost_per_token"] = prices[1]
    return {"model_name": name, "litellm_params": params, "model_info": info or {}}


@pytest.fixture
def state_router(monkeypatch) -> litellm.Router:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    return litellm.Router(
        model_list=[
            _deployment(
                "state-primary",
                "openai/state-primary",
                (0.0001, 0.001),
                {"data_class": "internal"},
            ),
            _deployment("state-mixed", "openai/state-mixed-priced", (0.0001, 0.001)),
            _deployment("state-mixed", "openai/state-mixed-unpriced", None),
            _deployment(
                "state-info",
                "openai/state-info",
                None,
                {"input_cost_per_token": 0.0002, "output_cost_per_token": 0.002},
            ),
            _deployment("state-mapped", "openai/gpt-4o-mini", None),
            _deployment("openai/*", "openai/*", None),
        ],
        model_group_alias={"state-alias": "state-primary"},
        fallbacks=[{"state-primary": ["elsewhere"]}],
    )


def test_the_live_gateway_state_reads_prices_classes_and_settings(state_router):
    state = bridge.LiteLLMGatewayState(
        lambda: state_router, lambda: {"fail_closed_budget_enforcement": True}
    )

    group = state.model_group("state-primary")
    assert group is not None
    assert (group.input_cost_per_token, group.output_cost_per_token) == (0.0001, 0.001)
    assert group.data_classes == ("internal",)
    assert state.model_group("never-configured") is None
    settings = state.settings()
    assert settings.fail_closed_budget_enforcement is True
    assert settings.disable_budget_reservation is False


@pytest.mark.parametrize(
    ("name", "priced"),
    [
        pytest.param("state-primary", True, id="explicit"),
        pytest.param("state-info", True, id="model-info"),
        pytest.param("state-mapped", True, id="cost-map"),
        pytest.param("state-mixed", False, id="one-unpriced-deployment"),
    ],
)
def test_a_group_is_priced_by_what_litellm_bills_each_deployment(
    state_router, name, priced
):
    state = bridge.LiteLLMGatewayState(lambda: state_router, dict)

    group = state.model_group(name)

    assert group is not None
    assert is_priced(group) is priced


@pytest.mark.parametrize(
    ("name", "concrete"),
    [
        pytest.param("state-primary", True, id="model-group"),
        pytest.param("state-alias", False, id="router-alias"),
        pytest.param("openai/*", False, id="wildcard"),
        pytest.param("openai/gpt-4o", False, id="wildcard-served"),
        pytest.param("state-primary,state-mixed", False, id="model-list"),
        pytest.param("never-configured", False, id="unknown"),
    ],
)
def test_only_a_configured_model_name_is_a_concrete_group(state_router, name, concrete):
    state = bridge.LiteLLMGatewayState(lambda: state_router, dict)

    assert state.is_concrete(name) is concrete


def test_a_deployment_id_is_not_a_concrete_group(state_router):
    state = bridge.LiteLLMGatewayState(lambda: state_router, dict)
    deployment_id = state_router.model_list[0]["model_info"]["id"]

    assert state.is_concrete(deployment_id) is False


def test_candidates_are_priced_one_deployment_at_a_time(state_router):
    state = bridge.LiteLLMGatewayState(lambda: state_router, dict)
    mixed = state_router.get_model_list(model_name="state-mixed")

    assert is_priced(state.candidate_group("state-mixed", mixed[:1]))
    assert not is_priced(state.candidate_group("state-mixed", mixed))


def test_the_gateway_state_without_a_router_serves_nothing():
    state = bridge.LiteLLMGatewayState(lambda: None, dict)

    assert state.model_group("anything") is None
    assert state.is_concrete("anything") is False
