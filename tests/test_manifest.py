from __future__ import annotations

import pytest

from budget_policy.manifest import (
    ManifestError,
    Posture,
    RoutePolicy,
    TeamPolicy,
    _build_team,
)


@pytest.mark.parametrize("tier", [0, 100, 150])
def test_the_manifest_rejects_a_tier_outside_the_range_it_owns(tier):
    with pytest.raises(ManifestError):
        _build_team(
            "test.yaml",
            "t",
            {
                "policy_epoch": "test-1",
                "envelope_usd": 1.0,
                "budget_duration": "7d",
                "warn_tiers": [tier],
            },
        )


@pytest.mark.parametrize("tier", [True, 75.5, "75"])
def test_the_manifest_rejects_values_that_only_coerce_to_integer_tiers(tier):
    with pytest.raises(ManifestError):
        _build_team(
            "test.yaml",
            "t",
            {
                "policy_epoch": "test-1",
                "envelope_usd": 1.0,
                "budget_duration": "7d",
                "warn_tiers": [tier],
            },
        )


def test_the_manifest_rejects_a_degradation_action_at_one_hundred_percent():
    with pytest.raises(ManifestError):
        _build_team(
            "test.yaml",
            "t",
            {
                "policy_epoch": "test-1",
                "envelope_usd": 1.0,
                "budget_duration": "7d",
                "warn_tiers": [75, 90],
                "routes": {"a": {"fallback": "b", "degrade_at": 100}},
            },
        )


def test_the_manifest_rejects_degradation_with_no_target():
    with pytest.raises(ManifestError):
        _build_team(
            "test.yaml",
            "t",
            {
                "envelope_usd": 1.0,
                "policy_epoch": "test-1",
                "budget_duration": "7d",
                "warn_tiers": [75],
                "routes": {"a": {"degrade_at": 90}},
            },
        )


@pytest.mark.parametrize("duration", ["1y", "banana", "7", "-1d", "0d"])
def test_the_manifest_rejects_a_budget_duration_litellm_cannot_parse(duration):
    with pytest.raises(ManifestError, match="budget_duration"):
        _build_team(
            "test.yaml",
            "t",
            {
                "envelope_usd": 1.0,
                "policy_epoch": "test-1",
                "budget_duration": duration,
                "warn_tiers": [75],
                "routes": {},
            },
        )


def test_a_route_cannot_be_constructed_with_a_threshold_it_cannot_act_on():
    with pytest.raises(AssertionError, match="no fallback"):
        RoutePolicy(
            route="a",
            fallback=None,
            degrade_at=90,
            on_counter_unavailable=Posture.BLOCK,
        )


@pytest.mark.parametrize("envelope", [0.0, -1.0, float("nan"), float("inf")])
def test_a_team_cannot_be_constructed_with_an_unusable_envelope(envelope):
    with pytest.raises(AssertionError, match="envelope"):
        TeamPolicy(
            team_id="t",
            policy_epoch="test-1",
            envelope_usd=envelope,
            budget_duration="7d",
            warn_tiers=(75,),
            default_posture=Posture.BLOCK,
            routes={},
        )


def test_a_team_cannot_be_constructed_with_a_tier_the_native_ceiling_owns():
    with pytest.raises(AssertionError, match="1..99"):
        TeamPolicy(
            team_id="t",
            policy_epoch="test-1",
            envelope_usd=1.0,
            budget_duration="7d",
            warn_tiers=(75, 100),
            default_posture=Posture.BLOCK,
            routes={},
        )
