from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any, Final

from .inputs import load_yaml
from .refusals import RuleId

HOUR_SECONDS: Final = 3600
DAY_SECONDS: Final = 24 * HOUR_SECONDS
LONGEST_MONTH_SECONDS: Final = 31 * DAY_SECONDS

WINDOW_SECONDS: Final[Mapping[str, int]] = {
    **{f"{hours}h": hours * HOUR_SECONDS for hours in (1, 2, 3, 4, 6, 8, 12, 24)},
    "hourly": HOUR_SECONDS,
    "1d": DAY_SECONDS,
    "daily": DAY_SECONDS,
    "7d": 7 * DAY_SECONDS,
    "1w": 7 * DAY_SECONDS,
    "weekly": 7 * DAY_SECONDS,
    "30d": LONGEST_MONTH_SECONDS,
    "1mo": LONGEST_MONTH_SECONDS,
    "monthly": LONGEST_MONTH_SECONDS,
}

MANIFEST_KEYS: Final = frozenset({"version", "data_class_order", "teams"})
TEAM_KEYS: Final = frozenset(
    {"policy_epoch", "envelope_usd", "budget_duration", "warn_tiers", "routes"}
)
ROUTE_KEYS: Final = frozenset({"fallback", "degrade_at", "warn_tiers"})


class ManifestError(Exception):
    def __init__(self, rule: RuleId, message: str) -> None:
        self.rule: Final = rule
        super().__init__(message)


@dataclass(frozen=True)
class RoutePolicy:
    route: str
    fallback: str | None
    degrade_at: int | None
    warn_tiers: tuple[int, ...]

    def __post_init__(self) -> None:
        assert self.route, "a route policy needs a route name"
        assert all(1 <= tier <= 99 for tier in self.warn_tiers), (
            f"route {self.route!r} has a warn tier outside 1..99"
        )
        assert self.fallback != self.route, (
            f"route {self.route!r} declares itself as its fallback"
        )
        if self.degrade_at is None:
            return
        assert self.fallback is not None, (
            f"route {self.route!r} declares degrade_at with no fallback"
        )
        assert 1 <= self.degrade_at <= 99, (
            f"route {self.route!r} degrade_at {self.degrade_at} is outside 1..99"
        )


@dataclass(frozen=True)
class TeamPolicy:
    team_id: str
    policy_epoch: str
    envelope_usd: float
    budget_duration: str
    warn_tiers: tuple[int, ...]
    routes: Mapping[str, RoutePolicy]

    def __post_init__(self) -> None:
        assert self.team_id, "a team policy needs a team id"
        assert self.policy_epoch, f"team {self.team_id!r} needs a policy epoch"
        assert isfinite(self.envelope_usd) and self.envelope_usd > 0, (
            f"team {self.team_id!r} envelope {self.envelope_usd} is not a positive "
            "finite number"
        )
        assert self.budget_duration in WINDOW_SECONDS, (
            f"team {self.team_id!r} budget_duration {self.budget_duration!r} does not "
            "tumble with LiteLLM's resets"
        )
        assert all(1 <= tier <= 99 for tier in self.warn_tiers), (
            f"team {self.team_id!r} has a warn tier outside 1..99"
        )

    def route_policy(self, route: str) -> RoutePolicy:
        declared = self.routes.get(route)
        if declared is not None:
            return declared
        return RoutePolicy(route=route, fallback=None, degrade_at=None, warn_tiers=())


class Manifest:
    def __init__(
        self,
        version: str,
        data_class_order: tuple[str, ...],
        teams: Mapping[str, TeamPolicy],
    ) -> None:
        self.version: Final = version
        self.data_class_order: Final = data_class_order
        self._teams: Final = dict(teams)

    @classmethod
    def from_file(cls, path: str | Path) -> Manifest:
        return cls.from_document(str(path), load_yaml(Path(path)))

    @classmethod
    def from_document(cls, source: str, document: Any) -> Manifest:
        if not isinstance(document, dict):
            raise _schema(source, "a manifest must be a YAML mapping")
        _require_known_keys(source, "the manifest", document, MANIFEST_KEYS)

        version = document.get("version")
        if not isinstance(version, str) or not version:
            raise _schema(source, "a manifest must carry a string 'version'")

        teams = document.get("teams")
        if not isinstance(teams, dict) or not teams:
            raise _schema(source, "'teams' must be a non-empty mapping")

        policies = {
            str(team_id): _build_team(source, str(team_id), entry)
            for team_id, entry in teams.items()
        }
        data_class_order = _data_class_order(
            source, document.get("data_class_order"), policies
        )
        return cls(version=version, data_class_order=data_class_order, teams=policies)

    def team_policy(self, team_id: str | None) -> TeamPolicy | None:
        if team_id is None:
            return None
        return self._teams.get(team_id)

    @property
    def team_ids(self) -> tuple[str, ...]:
        return tuple(self._teams)

    @property
    def teams(self) -> tuple[TeamPolicy, ...]:
        return tuple(self._teams.values())

    def sensitivity(self, data_class: str) -> int | None:
        if data_class not in self.data_class_order:
            return None
        return self.data_class_order.index(data_class)


def _build_team(source: str, team_id: str, entry: Any) -> TeamPolicy:
    where = f"team {team_id!r}"
    if not isinstance(entry, dict):
        raise _schema(source, f"{where} must be a mapping")
    _require_known_keys(source, where, entry, TEAM_KEYS)

    policy_epoch = entry.get("policy_epoch")
    if not isinstance(policy_epoch, str) or not policy_epoch:
        raise _schema(source, f"{where} must declare a string 'policy_epoch'")

    routes_entry = entry.get("routes") or {}
    if not isinstance(routes_entry, dict):
        raise _schema(source, f"{where} 'routes' must be a mapping")

    return TeamPolicy(
        team_id=team_id,
        policy_epoch=policy_epoch,
        envelope_usd=_envelope(source, where, entry.get("envelope_usd")),
        budget_duration=_budget_duration(source, where, entry.get("budget_duration")),
        warn_tiers=_tiers(source, where, entry.get("warn_tiers")),
        routes={
            str(route): _build_route(source, team_id, str(route), route_entry)
            for route, route_entry in routes_entry.items()
        },
    )


def _build_route(source: str, team_id: str, route: str, entry: Any) -> RoutePolicy:
    where = f"team {team_id!r} route {route!r}"
    if entry is None:
        entry = {}
    if not isinstance(entry, dict):
        raise _schema(source, f"{where} must be a mapping")
    _require_known_keys(source, where, entry, ROUTE_KEYS)

    fallback = entry.get("fallback")
    if fallback is not None and (not isinstance(fallback, str) or not fallback):
        raise _schema(source, f"{where} 'fallback' must be a model group name")
    if fallback == route:
        raise ManifestError(
            RuleId.FALLBACK_DISTINCT,
            f"{source}: {where} declares itself as its fallback",
        )

    degrade_at = entry.get("degrade_at")
    if degrade_at is not None:
        degrade_at = _tier_value(source, where, degrade_at)
        if fallback is None:
            raise _schema(source, f"{where} declares 'degrade_at' with no 'fallback'")

    return RoutePolicy(
        route=route,
        fallback=fallback,
        degrade_at=degrade_at,
        warn_tiers=_tiers(source, where, entry.get("warn_tiers")),
    )


def _data_class_order(
    source: str, value: Any, teams: Mapping[str, TeamPolicy]
) -> tuple[str, ...]:
    declares_fallback = any(
        route.fallback is not None
        for team in teams.values()
        for route in team.routes.values()
    )
    if value is None:
        if declares_fallback:
            raise _schema(
                source,
                "'data_class_order' is required when a route declares a fallback, "
                "listing data classes least restricted first",
            )
        return ()
    if not isinstance(value, list) or not value:
        raise _schema(source, "'data_class_order' must be a non-empty list")
    if not all(isinstance(entry, str) and entry for entry in value):
        raise _schema(source, "'data_class_order' entries must be non-empty strings")
    if len(set(value)) != len(value):
        raise _schema(source, "'data_class_order' lists a data class twice")
    return tuple(value)


def _budget_duration(source: str, where: str, value: Any) -> str:
    if not isinstance(value, str) or value not in WINDOW_SECONDS:
        raise ManifestError(
            RuleId.DURATION_TUMBLES,
            f"{source}: {where} declares budget_duration {value!r}. Accepted forms "
            f"are {', '.join(WINDOW_SECONDS)}, the windows that tumble with "
            "LiteLLM's resets",
        )
    return value


def _tiers(source: str, where: str, value: Any) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _schema(source, f"{where} 'warn_tiers' must be a list")
    return tuple(sorted({_tier_value(source, where, tier) for tier in value}))


def _tier_value(source: str, where: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _schema(source, f"{where} has a non-integer tier {value!r}")
    if not 1 <= value <= 99:
        raise _schema(
            source,
            f"{where} tier {value} is outside 1..99. 100% belongs to LiteLLM's ceiling",
        )
    return value


def _envelope(source: str, where: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _schema(source, f"{where} must declare a numeric 'envelope_usd'")
    envelope = float(value)
    if not isfinite(envelope) or envelope <= 0:
        raise _schema(
            source, f"{where} 'envelope_usd' must be a positive finite number"
        )
    return envelope


def _require_known_keys(
    source: str, where: str, entry: Mapping[Any, Any], known: frozenset[str]
) -> None:
    unknown = sorted(str(key) for key in entry if key not in known)
    if unknown:
        raise _schema(
            source,
            f"{where} declares unknown key {', '.join(repr(key) for key in unknown)}. "
            f"Known keys are {', '.join(sorted(known))}",
        )


def _schema(source: str, message: str) -> ManifestError:
    return ManifestError(RuleId.MANIFEST_SCHEMA, f"{source}: {message}")
