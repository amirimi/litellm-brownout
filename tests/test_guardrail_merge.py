"""Guardrails survive a route rewrite.

LiteLLM merges model-level guardrails into the request immediately before the
pre-call hook, keyed on the model the caller asked for. Rewriting the model
inside the hook therefore drops the target route's guardrails unless the merge
is re-run. The merge is a set union, so re-running it is idempotent and can only
add controls.
"""

from __future__ import annotations

from typing import Any, Final

import pytest

from budget_policy.adapter import _apply, _remerge_guardrails
from budget_policy.manifest import Posture, RoutePolicy, TeamPolicy
from budget_policy.policy import Action, Decision, Reason

PRIMARY_GUARDRAIL: Final = "pii-policy"
FALLBACK_GUARDRAIL: Final = "residency-policy"


class FakeRouter:
    """The slice of ``Router`` the guardrail merge actually reads."""

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
def router() -> FakeRouter:
    return FakeRouter(
        {
            "budget-lab-primary": [PRIMARY_GUARDRAIL],
            "budget-lab-degraded": [FALLBACK_GUARDRAIL],
        }
    )


@pytest.fixture
def team() -> TeamPolicy:
    route = RoutePolicy(
        route="budget-lab-primary",
        fallback="budget-lab-degraded",
        degrade_at=90,
        on_counter_unavailable=Posture.BLOCK,
    )
    return TeamPolicy(
        team_id="budget-lab",
        policy_epoch="test-1",
        envelope_usd=1000.0,
        budget_duration="7d",
        warn_tiers=(75, 90),
        default_posture=Posture.BLOCK,
        routes={route.route: route},
    )


def degrade_decision() -> Decision:
    return Decision(
        action=Action.DEGRADE,
        served_route="budget-lab-degraded",
        degraded_from="budget-lab-primary",
        tier=90,
        tiers_crossed=(90,),
        reason=Reason.DEGRADE_TIER_REACHED,
        utilisation=0.92,
    )


def test_a_rewrite_keeps_both_routes_guardrails(monkeypatch, router, team):
    """The merge is a union, so the primary's guardrail survives beside the target's."""
    monkeypatch.setattr("budget_policy.adapter.router", lambda: router)

    data = {
        "model": "budget-lab-primary",
        "messages": [],
        "metadata": {"guardrails": [PRIMARY_GUARDRAIL]},
    }
    applied = _apply(data, degrade_decision(), team)

    assert applied["model"] == "budget-lab-degraded"
    assert set(applied["metadata"]["guardrails"]) == {
        PRIMARY_GUARDRAIL,
        FALLBACK_GUARDRAIL,
    }


def test_remerging_twice_changes_nothing(monkeypatch, router):
    """The union is idempotent, so a re-run can only ever add controls."""
    monkeypatch.setattr("budget_policy.adapter.router", lambda: router)

    data = {
        "model": "budget-lab-degraded",
        "messages": [],
        "metadata": {"guardrails": [PRIMARY_GUARDRAIL]},
    }
    once = _remerge_guardrails(data)
    twice = _remerge_guardrails(once)

    assert set(once["metadata"]["guardrails"]) == set(twice["metadata"]["guardrails"])


def test_an_admitted_request_is_not_rewritten(monkeypatch, router, team):
    """No rewrite means no guardrail change."""
    monkeypatch.setattr("budget_policy.adapter.router", lambda: router)

    data = {
        "model": "budget-lab-primary",
        "messages": [],
        "metadata": {"guardrails": [PRIMARY_GUARDRAIL]},
    }
    admitted = Decision(
        action=Action.ADMIT,
        served_route="budget-lab-primary",
        degraded_from=None,
        tier=75,
        tiers_crossed=(75,),
        reason=Reason.TIER_REACHED,
        utilisation=0.8,
    )
    applied = _apply(data, admitted, team)

    assert applied["model"] == "budget-lab-primary"
    assert applied["metadata"]["guardrails"] == [PRIMARY_GUARDRAIL]
