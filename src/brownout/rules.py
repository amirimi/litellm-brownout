from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import isfinite

from .manifest import Manifest, RoutePolicy
from .refusals import RuleId, Violation


@dataclass(frozen=True)
class ModelGroup:
    name: str
    input_cost_per_token: float | None
    output_cost_per_token: float | None
    data_classes: tuple[str | None, ...]


@dataclass(frozen=True)
class GatewaySettings:
    fail_closed_budget_enforcement: bool
    disable_budget_reservation: bool


@dataclass(frozen=True)
class GatewayFile:
    groups: Mapping[str, ModelGroup]
    settings: GatewaySettings
    native_budget_alerts: bool
    redis_configured: bool


GroupLookup = Callable[[str], ModelGroup | None]


def is_priced(group: ModelGroup) -> bool:
    return has_usable_prices(group.input_cost_per_token, group.output_cost_per_token)


def has_usable_prices(input_cost: float | None, output_cost: float | None) -> bool:
    if input_cost is None or output_cost is None:
        return False
    prices = (input_cost, output_cost)
    if not all(isfinite(price) and price >= 0 for price in prices):
        return False
    return any(price > 0 for price in prices)


def route_rules(
    manifest: Manifest, policy: RoutePolicy, lookup: GroupLookup
) -> list[Violation]:
    violations = _served_and_priced(policy.route, lookup)
    if policy.fallback is None:
        return violations
    violations += _served_and_priced(policy.fallback, lookup)
    if violations:
        return violations
    route_class = _declared_class(manifest, policy.route, lookup, violations)
    fallback_class = _declared_class(manifest, policy.fallback, lookup, violations)
    if route_class is None or fallback_class is None:
        return violations
    route_rank = manifest.sensitivity(route_class)
    fallback_rank = manifest.sensitivity(fallback_class)
    assert route_rank is not None and fallback_rank is not None
    if fallback_rank < route_rank:
        violations.append(
            Violation(
                RuleId.FALLBACK_DATA_CLASS,
                policy.route,
                f"fallback {policy.fallback!r} carries data class {fallback_class!r}, "
                f"less restricted than the route's {route_class!r}",
            )
        )
    return violations


def settings_rules(settings: GatewaySettings) -> list[Violation]:
    violations: list[Violation] = []
    if not settings.fail_closed_budget_enforcement:
        violations.append(
            Violation(
                RuleId.FAIL_CLOSED_ON,
                "general_settings.fail_closed_budget_enforcement",
                "must be true, or a hold that does not fit shrinks to the headroom and a "
                "window can close above its envelope",
            )
        )
    if settings.disable_budget_reservation:
        violations.append(
            Violation(
                RuleId.RESERVATION_ON,
                "general_settings.disable_budget_reservation",
                "must be absent or false, or admission becomes a plain counter read "
                "that concurrent requests all pass",
            )
        )
    return violations


def file_rules(manifest: Manifest, gateway: GatewayFile) -> list[Violation]:
    violations = settings_rules(gateway.settings)
    if gateway.native_budget_alerts:
        violations.append(
            Violation(
                RuleId.NO_NATIVE_BUDGET_ALERTS,
                "general_settings.alert_types",
                "includes budget_alerts, or is absent while alerting is on, which makes "
                "LiteLLM's 85% and 95% budget alerter a second owner of tier "
                "announcements. List the alert types the gateway wants without "
                "budget_alerts",
            )
        )
    if not gateway.redis_configured:
        violations.append(
            Violation(
                RuleId.REDIS_CONFIGURED,
                "general_settings.coordination_redis",
                "is unset. The spend counter and the plugin's claims need it, and a Redis "
                "cache does not provide it on every supported LiteLLM release",
            )
        )
    for team in manifest.teams:
        for policy in team.routes.values():
            violations.extend(
                Violation(
                    violation.rule,
                    f"{team.team_id}/{violation.subject}",
                    violation.message,
                )
                for violation in route_rules(manifest, policy, gateway.groups.get)
            )
    return violations


def _served_and_priced(name: str, lookup: GroupLookup) -> list[Violation]:
    group = lookup(name)
    if group is None:
        return [
            Violation(
                RuleId.ROUTE_SERVED, name, "is not a model group the gateway serves"
            )
        ]
    if not is_priced(group):
        return [
            Violation(
                RuleId.ROUTE_PRICED,
                name,
                "has no input and output price, so LiteLLM cannot reserve against it",
            )
        ]
    return []


def _declared_class(
    manifest: Manifest, name: str, lookup: GroupLookup, violations: list[Violation]
) -> str | None:
    group = lookup(name)
    assert group is not None
    classes = set(group.data_classes)
    if len(classes) != 1:
        violations.append(
            Violation(
                RuleId.DATA_CLASS_DECLARED,
                name,
                f"deployments declare data classes {sorted(map(str, classes))}, "
                "and every deployment of a group must declare the same one",
            )
        )
        return None
    data_class = classes.pop()
    if data_class is None:
        violations.append(
            Violation(
                RuleId.DATA_CLASS_DECLARED,
                name,
                "has no model_info.data_class. A route with a fallback and its "
                "fallback each need one from data_class_order "
                f"{list(manifest.data_class_order)}",
            )
        )
        return None
    if manifest.sensitivity(data_class) is None:
        violations.append(
            Violation(
                RuleId.DATA_CLASS_DECLARED,
                name,
                f"declares data class {data_class!r}, which is not in the manifest's "
                f"data_class_order {list(manifest.data_class_order)}",
            )
        )
        return None
    return data_class
