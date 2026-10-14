from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class RuleId(StrEnum):
    MANIFEST_SCHEMA = "manifest-schema"
    DURATION_TUMBLES = "duration-tumbles"
    FALLBACK_DISTINCT = "fallback-distinct"
    ROUTE_SERVED = "route-served"
    ROUTE_PRICED = "route-priced"
    DATA_CLASS_DECLARED = "data-class-declared"
    FALLBACK_DATA_CLASS = "fallback-data-class"
    FAIL_CLOSED_ON = "fail-closed-on"
    RESERVATION_ON = "reservation-on"
    NO_NATIVE_BUDGET_ALERTS = "no-native-budget-alerts"
    REDIS_CONFIGURED = "redis-configured"
    SUPPORTED_LITELLM = "supported-litellm"
    NO_CALLER_ROUTING = "no-caller-routing"
    TEAM_IN_MANIFEST = "team-in-manifest"
    KEY_HAS_NO_BUDGET = "key-has-no-budget"
    MODEL_PRICED = "model-priced"
    CEILING_MATCHES_ENVELOPE = "ceiling-matches-envelope"
    SPEND_READABLE = "spend-readable"
    DURATION_MATCHES = "duration-matches"
    TEAM_EXISTS = "team-exists"
    FALLBACK_IN_TEAM_MODELS = "fallback-in-team-models"
    MEMBER_HAS_NO_BUDGET = "member-has-no-budget"
    MODEL_IS_A_GROUP = "model-is-a-group"


@dataclass(frozen=True)
class Violation:
    rule: RuleId
    subject: str
    message: str


class RefusalReason(StrEnum):
    CALLER_ROUTING_NOT_PERMITTED = "caller_routing_not_permitted"
    TEAM_NOT_IN_MANIFEST = "team_not_in_manifest"
    KEY_HAS_OWN_BUDGET = "key_has_own_budget"
    UNPRICED_MODEL = "unpriced_model"
    BUDGET_CEILING_DRIFT = "budget_ceiling_drift"
    GATEWAY_RULE_VIOLATED = "gateway_rule_violated"
    SPEND_COUNTER_UNREADABLE = "spend_counter_unreadable"
    MODEL_NOT_A_GROUP = "model_not_a_group"


@dataclass(frozen=True)
class Refusal:
    rule: RuleId
    reason: RefusalReason
    message: str
    detail: Mapping[str, Any]
    incident_subject: str | None
