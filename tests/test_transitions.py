from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brownout import transitions
from brownout.manifest import WINDOW_SECONDS
from brownout.policy import (
    Decision,
    Degraded,
    Rejected,
    Rejection,
    Served,
    WantsDegrade,
)

from .support import FakeStore, RecordingSink


class OffsetSchedule:
    def window_end(self, budget_duration: str):
        return datetime(
            2026, 10, 12, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))
        )


def decision(team_tiers=(75,), route_tiers=()) -> Decision:
    return Decision(
        route="budget-lab-primary",
        team_tiers=team_tiers,
        route_tiers=route_tiers,
        spend_usd=800.0,
        wants=WantsDegrade("budget-lab-degraded", 75),
    )


def test_a_window_id_is_the_reset_instant_in_utc():
    assert transitions.window_id(OffsetSchedule(), "7d") == "2026-10-12T06:30:00+00:00"


@pytest.mark.parametrize("duration", sorted(WINDOW_SECONDS))
def test_a_claim_outlives_its_window(duration):
    assert transitions.window_ttl(duration) == 2 * WINDOW_SECONDS[duration]


def test_each_transition_has_its_own_key(team):
    keys = [
        transition.key
        for transition in transitions.request_transitions(
            team,
            "w",
            decision(route_tiers=(60,)),
            Degraded("budget-lab-primary", "budget-lab-degraded", 75),
        )
    ]

    epoch = team.policy_epoch
    assert keys == [
        f"tier:budget-lab:{epoch}:w:team:75",
        f"tier:budget-lab:{epoch}:w:route:budget-lab-primary:60",
        f"degraded:budget-lab:{epoch}:w:budget-lab-primary",
    ]


def test_a_rejection_is_keyed_on_its_reason(team):
    outcome = Rejected(
        "budget-lab-primary",
        "budget-lab-degraded",
        75,
        Rejection.OUTSIDE_CALLER_ALLOWLIST,
    )

    keys = [
        transition.key
        for transition in transitions.request_transitions(
            team, "w", decision(), outcome
        )
    ]

    assert keys[-1] == (
        f"fallback_rejected:budget-lab:{team.policy_epoch}:w:budget-lab-primary:"
        "outside_caller_allowlist"
    )


def test_a_served_request_announces_tiers_only(team):
    served = transitions.request_transitions(
        team, "w", decision(), Served("budget-lab-primary")
    )

    assert [transition.event.outcome for transition in served] == ["served"]


async def test_publishing_announces_each_claim_once(team):
    store = FakeStore()
    sink = RecordingSink()
    batch = transitions.request_transitions(
        team, "w", decision(), Served("budget-lab-primary")
    )

    await transitions.publish(store, sink, 60, batch)
    await transitions.publish(store, sink, 60, batch)

    assert len(sink.tiers) == 1


async def test_an_incident_is_keyed_on_reason_subject_and_window():
    transition = transitions.incident("unpriced_model", "team:t", "w", {"team_id": "t"})

    assert transition.key == "incident:unpriced_model:team:t:w"
    assert transition.event.as_payload()["owner"] == "platform"
