from __future__ import annotations

import pytest

from brownout.manifest import RoutePolicy, TeamPolicy
from brownout.policy import (
    Admit,
    Degraded,
    Facts,
    Permission,
    Rejected,
    Rejection,
    Served,
    WantsDegrade,
    decide,
    resolve,
)


def facts_at(team: TeamPolicy, route: RoutePolicy, percent: float) -> Facts:
    return Facts(team=team, route=route, spend_usd=team.envelope_usd * percent / 100.0)


def test_below_the_first_tier_reaches_nothing(team, degrading_route):
    decision = decide(facts_at(team, degrading_route, 74.99))

    assert decision.team_tiers == ()
    assert decision.budget_tier is None
    assert decision.wants == Admit()


def test_every_team_tier_reached_is_reported_ascending(team, degrading_route):
    assert decide(facts_at(team, degrading_route, 75.0)).team_tiers == (75,)

    decision = decide(facts_at(team, degrading_route, 92.0))

    assert decision.team_tiers == (75, 90)
    assert decision.budget_tier == 90


def test_a_route_degrades_at_its_declared_threshold(team, degrading_route):
    assert decide(facts_at(team, degrading_route, 89.9)).wants == Admit()
    assert decide(facts_at(team, degrading_route, 90.0)).wants == WantsDegrade(
        target="budget-lab-degraded", degrade_at=90
    )


@pytest.mark.parametrize("percent", [75.0, 90.0, 99.9])
def test_a_route_without_a_degrade_threshold_never_wants_one(
    team, strict_route, percent
):
    assert decide(facts_at(team, strict_route, percent)).wants == Admit()


def test_a_degrade_threshold_is_not_a_team_tier(team):
    route = RoutePolicy(
        route="budget-lab-primary",
        fallback="budget-lab-degraded",
        degrade_at=85,
        warn_tiers=(),
    )

    decision = decide(facts_at(team, route, 86.0))

    assert decision.team_tiers == (75,)
    assert decision.route_tiers == ()
    assert decision.wants == WantsDegrade(target="budget-lab-degraded", degrade_at=85)


def test_route_tiers_are_declared_and_reported_separately(team):
    route = RoutePolicy(
        route="chat", fallback=None, degrade_at=None, warn_tiers=(60, 80)
    )

    decision = decide(facts_at(team, route, 70.0))

    assert decision.team_tiers == ()
    assert decision.route_tiers == (60,)
    assert decision.route_tier == 60


def test_an_admitted_route_is_served_as_requested(team, degrading_route):
    decision = decide(facts_at(team, degrading_route, 10.0))

    assert resolve(decision, None) == Served("budget-lab-primary")


@pytest.mark.parametrize(
    ("permission", "expected"),
    [
        (
            Permission.PERMITTED,
            Degraded("budget-lab-primary", "budget-lab-degraded", 90),
        ),
        (
            Permission.DENIED,
            Rejected(
                "budget-lab-primary",
                "budget-lab-degraded",
                90,
                Rejection.OUTSIDE_CALLER_ALLOWLIST,
            ),
        ),
        (
            Permission.UNAVAILABLE,
            Rejected(
                "budget-lab-primary",
                "budget-lab-degraded",
                90,
                Rejection.ENTITLEMENT_CHECK_UNAVAILABLE,
            ),
        ),
    ],
)
def test_a_wanted_downgrade_resolves_on_the_permission_answer(
    team, degrading_route, permission, expected
):
    decision = decide(facts_at(team, degrading_route, 95.0))

    assert resolve(decision, permission) == expected


def test_an_admitted_route_takes_no_permission_answer(team, degrading_route):
    with pytest.raises(AssertionError, match="no permission"):
        resolve(decide(facts_at(team, degrading_route, 10.0)), Permission.PERMITTED)


def test_a_wanted_downgrade_needs_a_permission_answer(team, degrading_route):
    with pytest.raises(AssertionError, match="permission answer"):
        resolve(decide(facts_at(team, degrading_route, 95.0)), None)


def test_spend_cannot_be_negative(team, degrading_route):
    with pytest.raises(AssertionError, match="negative"):
        Facts(team=team, route=degrading_route, spend_usd=-1.0)
