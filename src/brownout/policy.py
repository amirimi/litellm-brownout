from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, assert_never

from .manifest import RoutePolicy, TeamPolicy


class Permission(StrEnum):
    PERMITTED = "permitted"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class Rejection(StrEnum):
    OUTSIDE_CALLER_ALLOWLIST = "outside_caller_allowlist"
    ENTITLEMENT_CHECK_UNAVAILABLE = "entitlement_check_unavailable"


@dataclass(frozen=True)
class Facts:
    team: TeamPolicy
    route: RoutePolicy
    spend_usd: float

    def __post_init__(self) -> None:
        assert self.spend_usd >= 0, f"spend {self.spend_usd} is negative"


@dataclass(frozen=True)
class Admit:
    pass


@dataclass(frozen=True)
class WantsDegrade:
    target: str
    degrade_at: int


@dataclass(frozen=True)
class Decision:
    route: str
    team_tiers: tuple[int, ...]
    route_tiers: tuple[int, ...]
    spend_usd: float
    wants: Admit | WantsDegrade

    @property
    def budget_tier(self) -> int | None:
        return _highest(self.team_tiers)

    @property
    def route_tier(self) -> int | None:
        return _highest(self.route_tiers)


@dataclass(frozen=True)
class Served:
    route: str


@dataclass(frozen=True)
class Degraded:
    source: str
    target: str
    degrade_at: int


@dataclass(frozen=True)
class Rejected:
    source: str
    target: str
    degrade_at: int
    rejection: Rejection


Outcome = Served | Degraded | Rejected


def decide(facts: Facts) -> Decision:
    percent_used: Final = facts.spend_usd / facts.team.envelope_usd * 100.0
    return Decision(
        route=facts.route.route,
        team_tiers=_reached(facts.team.warn_tiers, percent_used),
        route_tiers=_reached(facts.route.warn_tiers, percent_used),
        spend_usd=facts.spend_usd,
        wants=_wants(facts.route, percent_used),
    )


def resolve(decision: Decision, permission: Permission | None) -> Outcome:
    match decision.wants:
        case Admit():
            assert permission is None, "an admitted route needs no permission check"
            return Served(decision.route)
        case WantsDegrade(target=target, degrade_at=degrade_at):
            assert permission is not None, "a downgrade needs a permission answer"
            match permission:
                case Permission.PERMITTED:
                    return Degraded(decision.route, target, degrade_at)
                case Permission.DENIED:
                    return Rejected(
                        decision.route,
                        target,
                        degrade_at,
                        Rejection.OUTSIDE_CALLER_ALLOWLIST,
                    )
                case Permission.UNAVAILABLE:
                    return Rejected(
                        decision.route,
                        target,
                        degrade_at,
                        Rejection.ENTITLEMENT_CHECK_UNAVAILABLE,
                    )
                case _:
                    assert_never(permission)
        case _:
            assert_never(decision.wants)


def served_route(outcome: Outcome) -> str:
    match outcome:
        case Served(route=route):
            return route
        case Degraded(target=target):
            return target
        case Rejected(source=source):
            return source
        case _:
            assert_never(outcome)


def outcome_name(outcome: Outcome) -> str:
    match outcome:
        case Served():
            return "served"
        case Degraded():
            return "degraded"
        case Rejected():
            return "rejected"
        case _:
            assert_never(outcome)


def _wants(route: RoutePolicy, percent_used: float) -> Admit | WantsDegrade:
    if route.degrade_at is None or percent_used < route.degrade_at:
        return Admit()
    assert route.fallback is not None
    return WantsDegrade(target=route.fallback, degrade_at=route.degrade_at)


def _reached(tiers: tuple[int, ...], percent_used: float) -> tuple[int, ...]:
    return tuple(tier for tier in sorted(tiers) if percent_used >= tier)


def _highest(tiers: tuple[int, ...]) -> int | None:
    if not tiers:
        return None
    return max(tiers)
