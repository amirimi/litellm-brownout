from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from typing import Final, assert_never

from .events import (
    Event,
    FallbackRejectedEvent,
    IncidentEvent,
    RouteDegradedEvent,
    TierEvent,
    TierScope,
)
from .manifest import WINDOW_SECONDS, TeamPolicy
from .policy import (
    Decision,
    Degraded,
    Outcome,
    Rejected,
    Served,
    outcome_name,
    served_route,
)
from .ports import ClaimStore, EventSink, ResetSchedule


@dataclass(frozen=True)
class Transition:
    key: str
    event: Event


def window_id(schedule: ResetSchedule, budget_duration: str) -> str:
    return schedule.window_end(budget_duration).astimezone(UTC).isoformat()


def window_ttl(budget_duration: str) -> int:
    return WINDOW_SECONDS[budget_duration] * 2


def incident(reason: str, subject: str, window: str, detail: dict) -> Transition:
    return Transition(
        key=f"incident:{reason}:{subject}:{window}",
        event=IncidentEvent(reason=reason, window=window, detail=detail),
    )


def request_transitions(
    team: TeamPolicy, window: str, decision: Decision, outcome: Outcome
) -> list[Transition]:
    prefix: Final = f"{team.team_id}:{team.policy_epoch}:{window}"
    served: Final = served_route(outcome)
    name: Final = outcome_name(outcome)
    transitions = [
        Transition(
            key=f"tier:{prefix}:team:{tier}",
            event=_tier_event(
                team, window, TierScope.TEAM, decision, tier, served, name
            ),
        )
        for tier in decision.team_tiers
    ]
    transitions += [
        Transition(
            key=f"tier:{prefix}:route:{decision.route}:{tier}",
            event=_tier_event(
                team,
                window,
                TierScope.ROUTE,
                decision,
                tier,
                served,
                name,
            ),
        )
        for tier in decision.route_tiers
    ]
    match outcome:
        case Served():
            pass
        case Degraded(source=source, target=target, degrade_at=degrade_at):
            transitions.append(
                Transition(
                    key=f"degraded:{prefix}:{source}",
                    event=RouteDegradedEvent(
                        team_id=team.team_id,
                        policy_epoch=team.policy_epoch,
                        window=window,
                        route=source,
                        served_route=target,
                        degrade_at=degrade_at,
                        spend_usd=decision.spend_usd,
                        envelope_usd=team.envelope_usd,
                    ),
                )
            )
        case Rejected(
            source=source, target=target, degrade_at=degrade_at, rejection=rejection
        ):
            transitions.append(
                Transition(
                    key=f"fallback_rejected:{prefix}:{source}:{rejection.value}",
                    event=FallbackRejectedEvent(
                        team_id=team.team_id,
                        policy_epoch=team.policy_epoch,
                        window=window,
                        route=source,
                        target=target,
                        rejection=rejection,
                        degrade_at=degrade_at,
                        spend_usd=decision.spend_usd,
                        envelope_usd=team.envelope_usd,
                    ),
                )
            )
        case _:
            assert_never(outcome)
    return transitions


async def publish(
    store: ClaimStore,
    sink: EventSink,
    ttl_seconds: int,
    transitions: Sequence[Transition],
) -> None:
    for transition in transitions:
        if await store.claim(transition.key, ttl_seconds):
            sink.emit(transition.event)


def _tier_event(
    team: TeamPolicy,
    window: str,
    scope: TierScope,
    decision: Decision,
    tier: int,
    served: str,
    name: str,
) -> TierEvent:
    return TierEvent(
        team_id=team.team_id,
        policy_epoch=team.policy_epoch,
        window=window,
        scope=scope,
        route=decision.route,
        tier=tier,
        served_route=served,
        outcome=name,
        spend_usd=decision.spend_usd,
        envelope_usd=team.envelope_usd,
    )
