from __future__ import annotations

import pytest

from budget_policy.manifest import (
    Posture,
    RoutePolicy,
    TeamPolicy,
)
from budget_policy.policy import Action, PolicyRequest, Reason, decide


def request_at(
    team: TeamPolicy,
    route: RoutePolicy,
    percent: float,
    already_signalled: frozenset[int] = frozenset(),
    selectable_fallback: str | None = "budget-lab-degraded",
) -> PolicyRequest:
    return PolicyRequest(
        team=team,
        route=route,
        projected_spend_usd=team.envelope_usd * percent / 100.0,
        already_signalled=already_signalled,
        selectable_fallback=selectable_fallback,
    )


def test_below_the_first_tier_is_admitted_silently(team, degrading_route):
    decision = decide(request_at(team, degrading_route, 74.99))

    assert decision.action is Action.ADMIT
    assert decision.tiers_crossed == ()
    assert decision.tier is None
    assert decision.reason is Reason.WITHIN_ENVELOPE


def test_crossing_a_tier_signals_once_per_window(team, degrading_route):
    first = decide(request_at(team, degrading_route, 75.0))
    assert first.tiers_crossed == (75,)
    assert first.tier == 75

    repeat = decide(
        request_at(team, degrading_route, 80.0, already_signalled=frozenset({75}))
    )
    assert repeat.tiers_crossed == ()
    assert repeat.tier == 75


def test_one_request_crossing_two_tiers_signals_both_ascending(team, degrading_route):
    decision = decide(request_at(team, degrading_route, 92.0))

    assert decision.tiers_crossed == (75, 90)
    assert list(decision.tiers_crossed) == sorted(decision.tiers_crossed)
    assert decision.tier == 90


def test_a_tier_does_not_unwind_inside_a_window(team, degrading_route):
    signalled = frozenset({75})

    fell_back = decide(
        request_at(team, degrading_route, 60.0, already_signalled=signalled)
    )
    assert fell_back.tiers_crossed == ()

    rose_again = decide(
        request_at(team, degrading_route, 76.0, already_signalled=signalled)
    )
    assert rose_again.tiers_crossed == ()


def test_a_degrading_route_degrades_at_its_declared_tier(team, degrading_route):
    below = decide(request_at(team, degrading_route, 89.9))
    assert below.action is Action.ADMIT
    assert below.served_route == "budget-lab-primary"
    assert below.degraded_from is None

    at_tier = decide(request_at(team, degrading_route, 90.0))
    assert at_tier.action is Action.DEGRADE
    assert at_tier.served_route == "budget-lab-degraded"
    assert at_tier.degraded_from == "budget-lab-primary"
    assert at_tier.reason is Reason.DEGRADE_TIER_REACHED


@pytest.mark.parametrize("percent", [75.0, 90.0, 95.0, 99.9])
def test_a_route_with_degradation_disabled_never_degrades(team, strict_route, percent):
    decision = decide(request_at(team, strict_route, percent, selectable_fallback=None))

    assert decision.action is Action.ADMIT
    assert decision.served_route == "budget-lab-strict"
    assert decision.degraded_from is None


def test_an_unselectable_fallback_serves_the_primary_rather_than_degrading(
    team, degrading_route
):
    decision = decide(request_at(team, degrading_route, 95.0, selectable_fallback=None))

    assert decision.action is Action.ADMIT
    assert decision.served_route == "budget-lab-primary"
    assert decision.reason is Reason.FALLBACK_REJECTED


def test_a_degrade_tier_the_team_did_not_list_still_signals(team):
    off_tier_route = RoutePolicy(
        route="budget-lab-primary",
        fallback="budget-lab-degraded",
        degrade_at=85,
        on_counter_unavailable=Posture.BLOCK,
    )

    decision = decide(
        request_at(team, off_tier_route, 86.0, already_signalled=frozenset({75}))
    )

    assert decision.action is Action.DEGRADE
    assert decision.tier == 85
    assert decision.tiers_crossed == (85,)


@pytest.mark.parametrize(
    ("posture", "expected_action"),
    [
        (Posture.BLOCK, Action.BLOCK),
        (Posture.DEGRADE, Action.DEGRADE),
        (Posture.ADMIT, Action.ADMIT),
    ],
)
def test_an_unreadable_counter_applies_the_declared_posture(
    team, posture, expected_action
):
    route = RoutePolicy(
        route="budget-lab-primary",
        fallback="budget-lab-degraded",
        degrade_at=90,
        on_counter_unavailable=posture,
    )
    decision = decide(
        PolicyRequest(
            team=team,
            route=route,
            projected_spend_usd=None,
            already_signalled=frozenset(),
            selectable_fallback="budget-lab-degraded",
        )
    )

    assert decision.action is expected_action
    assert decision.reason is Reason.COUNTER_UNAVAILABLE


def test_a_degrade_posture_with_no_valid_target_blocks(team):
    route = RoutePolicy(
        route="budget-lab-primary",
        fallback="budget-lab-degraded",
        degrade_at=90,
        on_counter_unavailable=Posture.DEGRADE,
    )
    decision = decide(
        PolicyRequest(
            team=team,
            route=route,
            projected_spend_usd=None,
            already_signalled=frozenset(),
            selectable_fallback=None,
        )
    )

    assert decision.action is Action.BLOCK
