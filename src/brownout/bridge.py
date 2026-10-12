from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime
from importlib.metadata import version as installed_version
from typing import Any, Final, Literal, assert_never

from fastapi import HTTPException, status
from litellm.proxy._types import ProxyException, UserAPIKeyAuth
from litellm.proxy.auth.auth_checks import (
    _can_object_call_model,
    _resolve_key_models_for_auth_check,
)
from litellm.proxy.common_utils.timezone_utils import get_budget_reset_time
from litellm.proxy.utils import _check_and_merge_model_level_guardrails

from .manifest import TeamPolicy
from .policy import (
    Decision,
    Degraded,
    Outcome,
    Permission,
    Rejected,
    Served,
    outcome_name,
    served_route,
)
from .ports import Caller, Guardrails
from .refusals import Refusal, RefusalReason, RuleId
from .rules import ROUTER_FALLBACK_KEYS, GatewaySettings, ModelGroup

logger: Final = logging.getLogger("brownout")

SUPPORTED_FLOOR: Final = (1, 95, 1)
REFUSE_WHOLE_FROM: Final = (1, 101, 0)
TESTED_CEILING: Final = (1, 104)

CHAT_COMPLETION_CALL_TYPES: Final = frozenset({"completion", "acompletion"})
SPEND_LOGS_METADATA_KEY: Final = "spend_logs_metadata"
FIELD_KEY: Final = "brownout"
OUTCOME_REFUSED: Final = "refused"
OUTCOME_DEGRADED: Final = "degraded"
RESERVED_METADATA_PREFIX: Final = "x-brownout-"
CALLER_ROUTING_KEYS: Final = ("fallbacks", "mock_testing_fallbacks")
METADATA_CONTAINERS: Final = ("metadata", "litellm_metadata")
REASON_DEGRADED: Final = "degrade_tier_reached"
REASON_REJECTED: Final = "fallback_rejected"

HEADER_ROUTE_SERVED: Final = "x-brownout-route-served"
HEADER_DEPLOYMENT_ID: Final = "x-brownout-deployment-id"
HEADER_BUDGET_TIER: Final = "x-brownout-budget-tier"
HEADER_ROUTE_TIER: Final = "x-brownout-route-tier"
HEADER_DEGRADED_FROM: Final = "x-brownout-degraded-from"
HEADER_DEGRADATION_REASON: Final = "x-brownout-degradation-reason"

RouterProvider = Callable[[], Any | None]
Boundary = Literal["refuse_whole", "shrink_to_fit"]


class UnsupportedLiteLLM(Exception):
    pass


def require_supported_litellm(release: str) -> None:
    parsed: Final = _release_tuple(release)
    if parsed < SUPPORTED_FLOOR:
        floor = ".".join(map(str, SUPPORTED_FLOOR))
        raise UnsupportedLiteLLM(
            f"{RuleId.SUPPORTED_LITELLM}: brownout supports LiteLLM {floor} and newer, "
            f"and this gateway runs {release}"
        )
    if parsed[:2] > TESTED_CEILING:
        ceiling = ".".join(map(str, TESTED_CEILING))
        logger.warning(
            "%s: LiteLLM %s is newer than the newest release brownout was tested on, %s.x",
            RuleId.SUPPORTED_LITELLM,
            release,
            ceiling,
        )
    _report_boundary(release)


def boundary_for(release: str) -> Boundary:
    if _release_tuple(release) < REFUSE_WHOLE_FROM:
        return "shrink_to_fit"
    return "refuse_whole"


def _report_boundary(release: str) -> None:
    boundary: Final = boundary_for(release)
    match boundary:
        case "refuse_whole":
            return
        case "shrink_to_fit":
            refusing = ".".join(map(str, REFUSE_WHOLE_FROM))
            logger.warning(
                "%s: LiteLLM %s shrinks a reservation that exceeds the remaining budget "
                "instead of refusing it, so a budget window can close above the envelope. "
                "Each request admitted with a shrunk hold can overrun it, by at most one "
                "request's cost under serial traffic. LiteLLM %s and newer refuse such a "
                "reservation",
                RuleId.SUPPORTED_LITELLM,
                release,
                refusing,
            )
        case _:
            assert_never(boundary)


def running_litellm() -> str:
    return installed_version("litellm")


def live_router() -> Any | None:
    from litellm.proxy.proxy_server import llm_router

    return llm_router


def live_general_settings() -> dict:
    from litellm.proxy.proxy_server import general_settings

    return general_settings


def live_spend_reader() -> Any:
    from litellm.proxy.proxy_server import get_current_spend

    return get_current_spend


def live_spend_counter_redis() -> Any | None:
    from litellm.proxy.proxy_server import spend_counter_cache

    return spend_counter_cache.redis_cache


def team_counter_key(team_id: str) -> str:
    return f"spend:team:{team_id}"


class LiteLLMSpendCounter:
    async def read(
        self, team_id: str, fallback_spend: float, max_budget: float
    ) -> float:
        return float(
            await live_spend_reader()(
                counter_key=team_counter_key(team_id),
                fallback_spend=fallback_spend,
                max_budget=max_budget,
            )
        )


class LiteLLMPermissions:
    def __init__(self, router: RouterProvider) -> None:
        self._router: Final = router

    def check(self, caller: Caller, model: str) -> Permission:
        answers: Final = (
            self._scope(caller, model, caller.key_models, "key"),
            self._scope(caller, model, caller.team_models, "team"),
        )
        if Permission.UNAVAILABLE in answers:
            return Permission.UNAVAILABLE
        if Permission.DENIED in answers:
            return Permission.DENIED
        return Permission.PERMITTED

    def _scope(
        self,
        caller: Caller,
        model: str,
        models: tuple[str, ...],
        object_type: Literal["key", "team"],
    ) -> Permission:
        try:
            _can_object_call_model(
                model=model,
                llm_router=self._router(),
                models=list(models),
                team_id=caller.team_id,
                object_type=object_type,
            )
        except ProxyException:
            return Permission.DENIED
        except Exception:
            logger.exception(
                "Permission check for model %s in %s scope did not complete",
                model,
                object_type,
            )
            return Permission.UNAVAILABLE
        return Permission.PERMITTED


class LiteLLMResetSchedule:
    def window_end(self, budget_duration: str) -> datetime:
        return get_budget_reset_time(budget_duration)


class LiteLLMGuardrails:
    def __init__(self, router: RouterProvider) -> None:
        self._router: Final = router

    def remerge(self, data: dict) -> dict:
        return _check_and_merge_model_level_guardrails(
            data=data, llm_router=self._router(), trust_client_model_info=False
        )


class LiteLLMGatewayState:
    def __init__(
        self, router: RouterProvider, general_settings: Callable[[], dict]
    ) -> None:
        self._router: Final = router
        self._general_settings: Final = general_settings

    def model_group(self, name: str) -> ModelGroup | None:
        router = self._router()
        if router is None:
            return None
        info = router.get_model_group_info(model_group=name)
        if info is None:
            return None
        return ModelGroup(
            name=name,
            input_cost_per_token=info.input_cost_per_token,
            output_cost_per_token=info.output_cost_per_token,
            data_classes=tuple(
                (deployment.get("model_info") or {}).get("data_class")
                for deployment in router.get_model_list(model_name=name) or []
            ),
        )

    def serves(self, name: str, team_id: str) -> bool:
        router = self._router()
        if router is None:
            return False
        return name in router.get_model_names(team_id=team_id)

    def settings(self) -> GatewaySettings:
        general = self._general_settings()
        router = self._router()
        configured: list[str] = []
        if router is not None:
            declared = {
                "fallbacks": router.fallbacks,
                "context_window_fallbacks": router.context_window_fallbacks,
                "content_policy_fallbacks": router.content_policy_fallbacks,
            }
            configured = [key for key in ROUTER_FALLBACK_KEYS if declared[key]]
        return GatewaySettings(
            fail_closed_budget_enforcement=general.get("fail_closed_budget_enforcement")
            is True,
            disable_budget_reservation=general.get("disable_budget_reservation")
            is True,
            router_fallback_keys=tuple(configured),
        )


class RedisClaimStore:
    def __init__(
        self, redis: Callable[[], Any | None], namespace: str = "brownout"
    ) -> None:
        self._redis: Final = redis
        self._namespace: Final = namespace
        self._local: Final[set[str]] = set()
        self._warned_local_only = False

    async def claim(self, key: str, ttl_seconds: int) -> bool:
        cache = self._cache()
        if cache is None:
            return self._claim_locally(key)
        try:
            claimed = await cache.async_set_cache(
                key=f"{self._namespace}:{key}", value=1, nx=True, ttl=ttl_seconds
            )
        except Exception:
            logger.exception(
                "Claim failed for %s, falling back to replica-local dedup", key
            )
            return self._claim_locally(key)
        return bool(claimed)

    def _cache(self) -> Any | None:
        try:
            return self._redis()
        except Exception:  # noqa: BLE001
            return None

    def _claim_locally(self, key: str) -> bool:
        if not self._warned_local_only:
            logger.warning(
                "No Redis is attached. Deduplication is replica-local, so a "
                "multi-replica gateway emits one event per replica per crossing"
            )
            self._warned_local_only = True
        if key in self._local:
            return False
        self._local.add(key)
        return True


def parse_caller(auth: UserAPIKeyAuth) -> Caller:
    team_max_budget = None
    if auth.team_max_budget is not None:
        team_max_budget = float(auth.team_max_budget)
    return Caller(
        team_id=auth.team_id,
        key_models=tuple(_resolve_key_models_for_auth_check(auth)),
        team_models=tuple(auth.team_models or []),
        key_budgets=_key_budgets(auth),
        team_max_budget=team_max_budget,
        team_spend=float(auth.team_spend or 0.0),
        own_hold_usd=_own_hold(auth),
    )


def _key_budgets(auth: UserAPIKeyAuth) -> tuple[str, ...]:
    declared = {
        "max_budget": auth.max_budget is not None,
        "model_max_budget": bool(auth.model_max_budget),
        "budget_limits": bool(auth.budget_limits),
    }
    return tuple(name for name, present in declared.items() if present)


def _own_hold(auth: UserAPIKeyAuth) -> float:
    reservation: Final = auth.budget_reservation
    if reservation is None or auth.team_id is None:
        return 0.0
    counter_key: Final = team_counter_key(auth.team_id)
    return sum(
        float(entry.get("reserved_cost") or 0.0)
        + float(entry.get("applied_adjustment") or 0.0)
        for entry in reservation.get("entries") or []
        if isinstance(entry, dict) and entry.get("counter_key") == counter_key
    )


def requested_model(data: dict) -> str:
    return str(data.get("model") or "")


def clear_field(data: dict) -> None:
    for container in METADATA_CONTAINERS:
        metadata = data.get(container)
        if not isinstance(metadata, dict):
            continue
        spend_logs_metadata = metadata.get(SPEND_LOGS_METADATA_KEY)
        if isinstance(spend_logs_metadata, dict) and FIELD_KEY in spend_logs_metadata:
            metadata[SPEND_LOGS_METADATA_KEY] = {
                key: value
                for key, value in spend_logs_metadata.items()
                if key != FIELD_KEY
            }


def write_field(data: dict, field: dict[str, Any]) -> None:
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        data["metadata"] = metadata
    existing: Final = metadata.get(SPEND_LOGS_METADATA_KEY)
    if isinstance(existing, dict):
        metadata[SPEND_LOGS_METADATA_KEY] = {**existing, FIELD_KEY: field}
    else:
        metadata[SPEND_LOGS_METADATA_KEY] = {FIELD_KEY: field}


def read_field(data: dict) -> dict[str, Any] | None:
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return None
    spend_logs_metadata = metadata.get(SPEND_LOGS_METADATA_KEY)
    if not isinstance(spend_logs_metadata, dict):
        return None
    field = spend_logs_metadata.get(FIELD_KEY)
    if not isinstance(field, dict):
        return None
    return field


def governed_field(
    team: TeamPolicy, decision: Decision, outcome: Outcome
) -> dict[str, Any]:
    field: dict[str, Any] = {
        "requested_route": decision.route,
        "outcome": outcome_name(outcome),
        "served_route": served_route(outcome),
    }
    optional = {
        "budget_tier": decision.budget_tier,
        "route_tier": decision.route_tier,
        "degradation_reason": _degradation_reason(outcome),
    }
    field.update(
        {key: str(value) for key, value in optional.items() if value is not None}
    )
    field["utilisation_percent"] = round(
        decision.spend_usd / team.envelope_usd * 100.0, 4
    )
    return field


def refused_field(data: dict, refusal: Refusal) -> dict[str, Any]:
    return {
        "requested_route": requested_model(data),
        "outcome": OUTCOME_REFUSED,
        "refusal": refusal.reason.value,
    }


def _degradation_reason(outcome: Outcome) -> str | None:
    match outcome:
        case Served():
            return None
        case Degraded():
            return REASON_DEGRADED
        case Rejected():
            return REASON_REJECTED
        case _:
            assert_never(outcome)


def caller_routing_keys(data: dict) -> list[str]:
    found: list[str] = [key for key in CALLER_ROUTING_KEYS if key in data]
    for container in METADATA_CONTAINERS:
        metadata = data.get(container)
        if not isinstance(metadata, dict):
            continue
        found.extend(
            f"{container}.{key}"
            for key in metadata
            if str(key).lower().startswith(RESERVED_METADATA_PREFIX)
            or key == "degraded_from"
        )
    return sorted(found)


def apply(data: dict, outcome: Outcome, guardrails: Guardrails) -> dict:
    match outcome:
        case Served() | Rejected():
            return data
        case Degraded(target=target):
            data["model"] = target
            return guardrails.remerge(data)
        case _:
            assert_never(outcome)


def headers(
    data: dict, litellm_call_info: dict[str, Any] | None
) -> dict[str, str] | None:
    field = read_field(data)
    if field is None or "served_route" not in field:
        return None
    degraded_from = None
    if field.get("outcome") == OUTCOME_DEGRADED:
        degraded_from = field.get("requested_route")
    result = {HEADER_ROUTE_SERVED: str(field["served_route"])}
    optional = {
        HEADER_DEPLOYMENT_ID: (litellm_call_info or {}).get("model_id"),
        HEADER_BUDGET_TIER: field.get("budget_tier"),
        HEADER_ROUTE_TIER: field.get("route_tier"),
        HEADER_DEGRADED_FROM: degraded_from,
        HEADER_DEGRADATION_REASON: field.get("degradation_reason"),
    }
    result.update(
        {name: str(value) for name, value in optional.items() if value is not None}
    )
    return result


def http_error(refusal: Refusal) -> HTTPException:
    match refusal.reason:
        case RefusalReason.CALLER_ROUTING_NOT_PERMITTED:
            status_code = status.HTTP_400_BAD_REQUEST
            error = refusal.reason.value
        case (
            RefusalReason.TEAM_NOT_IN_MANIFEST
            | RefusalReason.KEY_HAS_OWN_BUDGET
            | RefusalReason.UNPRICED_MODEL
            | RefusalReason.BUDGET_CEILING_DRIFT
            | RefusalReason.GATEWAY_RULE_VIOLATED
            | RefusalReason.SPEND_COUNTER_UNREADABLE
        ):
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            error = "brownout_unavailable"
        case _:
            assert_never(refusal.reason)
    return HTTPException(
        status_code=status_code,
        detail={
            "error": error,
            "reason": refusal.reason.value,
            "rule": refusal.rule.value,
            **refusal.detail,
            "message": refusal.message,
        },
    )


def _release_tuple(release: str) -> tuple[int, int, int]:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", release)
    if match is None:
        raise UnsupportedLiteLLM(
            f"{RuleId.SUPPORTED_LITELLM}: cannot read the LiteLLM release {release!r}"
        )
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
