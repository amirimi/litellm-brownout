"""Team manifests: the budget, its tiers, and each route's degradation policy.

The budget is a single owner-approved team total accumulating across every
route the team runs. It is materialised as LiteLLM's native ``max_budget``, so
the native 429 owns admission at 100% and this component owns every configurable
tier below it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Any, Final

import yaml
from litellm.litellm_core_utils.duration_parser import duration_in_seconds


class ManifestError(Exception):
    """The manifest file itself is unusable."""


class Posture(StrEnum):
    """What a route does when the spend counter cannot be read."""

    BLOCK = "block"
    DEGRADE = "degrade"
    ADMIT = "admit"


@dataclass(frozen=True)
class RoutePolicy:
    """A route's degradation policy.

    ``degrade_at`` set with no ``fallback`` is not a state this type admits. The
    decision reads ``degrade_at`` alone, so a route that could reach a threshold
    with nothing to degrade to would silently keep serving the primary while
    reporting a degrade tier. The loader reports that as a manifest error and
    the assertion catches any construction that bypasses it.
    """

    route: str
    fallback: str | None
    degrade_at: int | None
    on_counter_unavailable: Posture

    def __post_init__(self) -> None:
        assert self.route, "a route policy needs a route name"
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
    default_posture: Posture
    routes: Mapping[str, RoutePolicy]

    def __post_init__(self) -> None:
        assert self.team_id, "a team policy needs a team id"
        assert self.policy_epoch, f"team {self.team_id!r} needs a policy epoch"
        assert isfinite(self.envelope_usd) and self.envelope_usd > 0, (
            f"team {self.team_id!r} envelope {self.envelope_usd} is not a positive "
            "finite number, so no threshold derived from it can be reached"
        )
        assert self.budget_duration, f"team {self.team_id!r} needs a budget duration"
        assert self.warn_tiers, f"team {self.team_id!r} needs at least one warn tier"
        assert all(1 <= tier <= 99 for tier in self.warn_tiers), (
            f"team {self.team_id!r} has a warn tier outside 1..99. 100% belongs to "
            "the native LiteLLM ceiling, not to this component"
        )

    def route_policy(self, route: str) -> RoutePolicy:
        """The declared policy for a route, or a non-degrading default.

        A route the manifest does not name still spends against the envelope, so
        it still earns tier events. It just has no declared degradation target
        and therefore never degrades.
        """
        declared = self.routes.get(route)
        if declared is not None:
            return declared
        return RoutePolicy(
            route=route,
            fallback=None,
            degrade_at=None,
            on_counter_unavailable=self.default_posture,
        )


class Manifest:
    """Every team the gateway enforces an envelope for."""

    def __init__(self, version: str, teams: Mapping[str, TeamPolicy]) -> None:
        self.version: Final = version
        self._teams: Final = dict(teams)

    @classmethod
    def from_file(cls, path: str | Path) -> Manifest:
        document = _load_yaml(path)
        version = document.get("version")
        if not isinstance(version, str) or not version:
            raise ManifestError(f"{path}: a manifest must carry a string 'version'")

        teams = document.get("teams")
        if not isinstance(teams, dict) or not teams:
            raise ManifestError(f"{path}: 'teams' must be a non-empty mapping")

        return cls(
            version=version,
            teams={
                team_id: _build_team(path, team_id, entry)
                for team_id, entry in teams.items()
            },
        )

    def team_policy(self, team_id: str | None) -> TeamPolicy | None:
        if team_id is None:
            return None
        return self._teams.get(team_id)

    @property
    def team_ids(self) -> tuple[str, ...]:
        return tuple(self._teams)


def _build_team(path: str | Path, team_id: str, entry: Any) -> TeamPolicy:
    if not isinstance(entry, dict):
        raise ManifestError(f"{path}: team {team_id!r} must be a mapping")

    policy_epoch = entry.get("policy_epoch")
    if not isinstance(policy_epoch, str) or not policy_epoch:
        raise ManifestError(
            f"{path}: team {team_id!r} must declare a string 'policy_epoch'"
        )

    envelope = _positive_float(path, team_id, entry, "envelope_usd")
    budget_duration = _budget_duration(path, team_id, entry.get("budget_duration"))

    warn_tiers = _tiers(path, team_id, entry.get("warn_tiers"))
    default_posture = _posture(
        path, team_id, entry.get("on_counter_unavailable", Posture.BLOCK.value)
    )

    routes_entry = entry.get("routes") or {}
    if not isinstance(routes_entry, dict):
        raise ManifestError(f"{path}: team {team_id!r} 'routes' must be a mapping")

    routes = {
        route: _build_route(path, team_id, route, route_entry, default_posture)
        for route, route_entry in routes_entry.items()
    }

    return TeamPolicy(
        team_id=team_id,
        policy_epoch=policy_epoch,
        envelope_usd=envelope,
        budget_duration=budget_duration,
        warn_tiers=warn_tiers,
        default_posture=default_posture,
        routes=routes,
    )


def _build_route(
    path: str | Path,
    team_id: str,
    route: str,
    entry: Any,
    default_posture: Posture,
) -> RoutePolicy:
    if entry is None:
        entry = {}
    if not isinstance(entry, dict):
        raise ManifestError(
            f"{path}: team {team_id!r} route {route!r} must be a mapping"
        )

    fallback = entry.get("fallback")
    if fallback is not None and not isinstance(fallback, str):
        raise ManifestError(
            f"{path}: team {team_id!r} route {route!r} 'fallback' must be a model name"
        )

    degrade_at = entry.get("degrade_at")
    if degrade_at is not None:
        degrade_at = _tier_value(path, team_id, degrade_at)
        if fallback is None:
            raise ManifestError(
                f"{path}: team {team_id!r} route {route!r} declares 'degrade_at' with no 'fallback'"
            )

    return RoutePolicy(
        route=route,
        fallback=fallback,
        degrade_at=degrade_at,
        on_counter_unavailable=_posture(
            path, team_id, entry.get("on_counter_unavailable", default_posture.value)
        ),
    )


def _budget_duration(path: str | Path, team_id: str, value: Any) -> str:
    """A duration LiteLLM's own parser accepts, checked while the manifest loads.

    The window id and every claim TTL are derived from this string on the
    request path, and the two derivations disagree about a value neither
    understands. ``duration_in_seconds`` raises, while the reset-time helper
    logs a warning and silently returns the next midnight. A typo would
    therefore give a team a daily policy window against a budget that resets on
    another schedule. Parsing it here is what keeps that out of the runtime.
    """
    if not isinstance(value, str) or not value:
        raise ManifestError(
            f"{path}: team {team_id!r} must declare a string 'budget_duration'"
        )
    try:
        seconds = duration_in_seconds(value)
    except ValueError:
        raise ManifestError(
            f"{path}: team {team_id!r} declares budget_duration {value!r}, which "
            "LiteLLM cannot parse. Use a form like '30s', '30m', '24h', '7d' or '1mo'"
        ) from None
    if seconds <= 0:
        raise ManifestError(
            f"{path}: team {team_id!r} declares budget_duration {value!r}, a window "
            "of no length. Every tier claim would expire before it deduplicated anything"
        )
    return value


def _tiers(path: str | Path, team_id: str, value: Any) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ManifestError(
            f"{path}: team {team_id!r} must declare a non-empty 'warn_tiers' list"
        )
    tiers = tuple(sorted({_tier_value(path, team_id, tier) for tier in value}))
    return tiers


def _tier_value(path: str | Path, team_id: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(
            f"{path}: team {team_id!r} has a non-integer tier {value!r}"
        )
    tier = value
    if not 1 <= tier <= 99:
        raise ManifestError(
            f"{path}: team {team_id!r} tier {tier} is outside 1..99. "
            "100% belongs to the native LiteLLM ceiling, not to this component"
        )
    return tier


def _posture(path: str | Path, team_id: str, value: Any) -> Posture:
    try:
        return Posture(str(value))
    except ValueError:
        raise ManifestError(
            f"{path}: team {team_id!r} declares posture {value!r}, expected one of "
            + ", ".join(posture.value for posture in Posture)
        ) from None


def _positive_float(path: str | Path, team_id: str, entry: dict, field: str) -> float:
    try:
        value = float(entry[field])
    except KeyError:
        raise ManifestError(f"{path}: team {team_id!r} is missing {field!r}") from None
    except (TypeError, ValueError):
        raise ManifestError(
            f"{path}: team {team_id!r} has a non-numeric {field!r}"
        ) from None
    if not isfinite(value):
        raise ManifestError(
            f"{path}: team {team_id!r} {field!r} is not a finite number, so every "
            "threshold derived from it would compare false"
        )
    if value <= 0:
        raise ManifestError(
            f"{path}: team {team_id!r} {field!r} must be greater than zero"
        )
    return value


def _load_yaml(path: str | Path) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except FileNotFoundError:
        raise ManifestError(f"{path}: manifest file not found") from None
    if not isinstance(document, dict):
        raise ManifestError(f"{path}: manifest must be a YAML mapping")
    return document
