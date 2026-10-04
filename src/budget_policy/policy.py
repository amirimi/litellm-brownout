from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .manifest import Posture, RoutePolicy, TeamPolicy


class Action(StrEnum):
    ADMIT = "admit"
    DEGRADE = "degrade"
    BLOCK = "block"


class Reason(StrEnum):
    WITHIN_ENVELOPE = "within_envelope"
    TIER_REACHED = "tier_reached"
    DEGRADE_TIER_REACHED = "degrade_tier_reached"
    FALLBACK_REJECTED = "fallback_rejected"
    COUNTER_UNAVAILABLE = "counter_unavailable"


@dataclass(frozen=True)
class PolicyRequest:
    team: TeamPolicy
    route: RoutePolicy
    projected_spend_usd: float | None
    already_signalled: frozenset[int]

    selectable_fallback: str | None


@dataclass(frozen=True)
class Decision:
    action: Action
    served_route: str
    degraded_from: str | None
    tier: int | None
    tiers_crossed: tuple[int, ...]
    reason: Reason
    utilisation: float | None


def decide(request: PolicyRequest) -> Decision:
    if request.projected_spend_usd is None:
        return _counter_unavailable(request)

    envelope: Final = request.team.envelope_usd
    utilisation: Final = request.projected_spend_usd / envelope
    percent_used: Final = utilisation * 100.0

    declared: Final = _declared_tiers(request)
    tiers_crossed: Final = tuple(
        tier
        for tier in declared
        if percent_used >= tier and tier not in request.already_signalled
    )
    tier: Final = _highest_tier_reached(declared, percent_used)

    if not _should_degrade(request, percent_used):
        if tier is None:
            reason = Reason.WITHIN_ENVELOPE
        else:
            reason = Reason.TIER_REACHED
        return Decision(
            action=Action.ADMIT,
            served_route=request.route.route,
            degraded_from=None,
            tier=tier,
            tiers_crossed=tiers_crossed,
            reason=reason,
            utilisation=utilisation,
        )

    if request.selectable_fallback is None:
        return Decision(
            action=Action.ADMIT,
            served_route=request.route.route,
            degraded_from=None,
            tier=tier,
            tiers_crossed=tiers_crossed,
            reason=Reason.FALLBACK_REJECTED,
            utilisation=utilisation,
        )

    return Decision(
        action=Action.DEGRADE,
        served_route=request.selectable_fallback,
        degraded_from=request.route.route,
        tier=tier,
        tiers_crossed=tiers_crossed,
        reason=Reason.DEGRADE_TIER_REACHED,
        utilisation=utilisation,
    )


def _should_degrade(request: PolicyRequest, percent_used: float) -> bool:
    degrade_at: Final = request.route.degrade_at
    if degrade_at is None or request.route.fallback is None:
        return False
    return percent_used >= degrade_at


def _declared_tiers(request: PolicyRequest) -> tuple[int, ...]:
    declared: Final = set(request.team.warn_tiers)
    if request.route.degrade_at is not None:
        declared.add(request.route.degrade_at)
    return tuple(sorted(declared))


def _highest_tier_reached(declared: tuple[int, ...], percent_used: float) -> int | None:
    reached: Final = [tier for tier in declared if percent_used >= tier]
    if not reached:
        return None
    return reached[-1]


def _counter_unavailable(request: PolicyRequest) -> Decision:
    posture: Final = request.route.on_counter_unavailable
    if posture is Posture.ADMIT:
        action, served, degraded_from = Action.ADMIT, request.route.route, None
    elif posture is Posture.DEGRADE and request.selectable_fallback is not None:
        action, served, degraded_from = (
            Action.DEGRADE,
            request.selectable_fallback,
            request.route.route,
        )
    else:
        action, served, degraded_from = Action.BLOCK, request.route.route, None

    return Decision(
        action=action,
        served_route=served,
        degraded_from=degraded_from,
        tier=None,
        tiers_crossed=(),
        reason=Reason.COUNTER_UNAVAILABLE,
        utilisation=None,
    )
