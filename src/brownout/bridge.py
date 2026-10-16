from __future__ import annotations

import copy
import logging
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import Any, Final, Literal, assert_never

from fastapi import HTTPException, status
from litellm import Router
from litellm.exceptions import BadRequestError
from litellm.litellm_core_utils.core_helpers import (
    get_metadata_variable_name_from_kwargs,
)
from litellm.proxy._types import ProxyException, UserAPIKeyAuth
from litellm.proxy.auth.auth_checks import (
    _can_object_call_model,
    _resolve_key_models_for_auth_check,
)
from litellm.proxy.common_utils.timezone_utils import get_budget_reset_time
from litellm.proxy.utils import _check_and_merge_model_level_guardrails

from .gateway_config import declared_data_class, price, settings_from_general
from .inputs import DocumentError
from .manifest import WILDCARD, TeamPolicy
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
from .rules import (
    UNPRICED,
    DeploymentPrices,
    GatewaySettings,
    ModelGroup,
    group_from_deployments,
)

logger: Final = logging.getLogger("brownout")

SUPPORTED_FLOOR: Final = (1, 101, 6)
TESTED_CEILING: Final = (1, 105)

SPEND_LOGS_METADATA_KEY: Final = "spend_logs_metadata"
ENVIRONMENT_KEY: Final = "supported_environments"
LITELLM_LOGGERS: Final = ("LiteLLM", "LiteLLM Router")
FIELD_KEY: Final = "brownout"
OUTCOME_REFUSED: Final = "refused"
OUTCOME_DEGRADED: Final = "degraded"
OUTCOME_ADMITTED: Final = "admitted"
KIND_KEY: Final = "kind"
CALLER_ROUTING_KEYS: Final = (
    "fallbacks",
    "context_window_fallbacks",
    "content_policy_fallbacks",
    "mock_testing_fallbacks",
)
NEUTRALISED_FALLBACK_KEYS: Final = (
    "fallbacks",
    "context_window_fallbacks",
    "content_policy_fallbacks",
)
METADATA_CONTAINERS: Final = ("metadata", "litellm_metadata")
DISABLE_FALLBACKS_KEY: Final = "disable_fallbacks"
TEAM_ID_KEY: Final = "user_api_key_team_id"
SERVED_ROUTE_KEY: Final = "served_route"
REASON_DEGRADED: Final = "degrade_tier_reached"
REASON_REJECTED: Final = "fallback_rejected"

HEADER_ROUTE_SERVED: Final = "x-brownout-route-served"
HEADER_DEPLOYMENT_ID: Final = "x-brownout-deployment-id"
HEADER_BUDGET_TIER: Final = "x-brownout-budget-tier"
HEADER_ROUTE_TIER: Final = "x-brownout-route-tier"
HEADER_DEGRADED_FROM: Final = "x-brownout-degraded-from"
HEADER_DEGRADATION_REASON: Final = "x-brownout-degradation-reason"

RouterProvider = Callable[[], Any | None]


class CallKind(StrEnum):
    TEXT = "text"
    EMBEDDING = "embedding"
    COMPACTION = "compaction"
    OTHER_MODEL = "other_model"
    NO_MODEL = "no_model"


CALL_KINDS: Final[Mapping[str, CallKind]] = {
    "acompletion": CallKind.TEXT,
    "atext_completion": CallKind.TEXT,
    "text_completion": CallKind.TEXT,
    "anthropic_messages": CallKind.TEXT,
    "aresponses": CallKind.TEXT,
    "agenerate_content": CallKind.TEXT,
    "agenerate_content_stream": CallKind.TEXT,
    "aembedding": CallKind.EMBEDDING,
    "acompact_responses": CallKind.COMPACTION,
    "image_generation": CallKind.OTHER_MODEL,
    "aimage_generation": CallKind.OTHER_MODEL,
    "aimage_edit": CallKind.OTHER_MODEL,
    "aspeech": CallKind.OTHER_MODEL,
    "transcription": CallKind.OTHER_MODEL,
    "atranscription": CallKind.OTHER_MODEL,
    "moderation": CallKind.OTHER_MODEL,
    "amoderation": CallKind.OTHER_MODEL,
    "rerank": CallKind.OTHER_MODEL,
    "arerank": CallKind.OTHER_MODEL,
    "aocr": CallKind.OTHER_MODEL,
    "avideo_generation": CallKind.OTHER_MODEL,
    "call_mcp_tool": CallKind.NO_MODEL,
    "aget_responses": CallKind.NO_MODEL,
    "adelete_responses": CallKind.NO_MODEL,
    "acancel_responses": CallKind.NO_MODEL,
    "alist_input_items": CallKind.NO_MODEL,
    "acreate_file": CallKind.NO_MODEL,
    "alist_fine_tuning_jobs": CallKind.NO_MODEL,
    "afile_content": CallKind.NO_MODEL,
    "afile_retrieve": CallKind.NO_MODEL,
    "afile_delete": CallKind.NO_MODEL,
    "avector_store_create": CallKind.NO_MODEL,
    "avector_store_retrieve": CallKind.NO_MODEL,
    "avector_store_list": CallKind.NO_MODEL,
    "avector_store_update": CallKind.NO_MODEL,
    "avector_store_delete": CallKind.NO_MODEL,
    "avector_store_search": CallKind.NO_MODEL,
    "avector_store_file_create": CallKind.NO_MODEL,
    "avector_store_file_list": CallKind.NO_MODEL,
    "avector_store_file_retrieve": CallKind.NO_MODEL,
    "avector_store_file_content": CallKind.NO_MODEL,
    "avector_store_file_update": CallKind.NO_MODEL,
    "avector_store_file_delete": CallKind.NO_MODEL,
    "acreate_container": CallKind.NO_MODEL,
    "alist_containers": CallKind.NO_MODEL,
    "aretrieve_container": CallKind.NO_MODEL,
    "adelete_container": CallKind.NO_MODEL,
    "aupload_container_file": CallKind.NO_MODEL,
    "alist_container_files": CallKind.NO_MODEL,
    "aretrieve_container_file": CallKind.NO_MODEL,
    "aretrieve_container_file_content": CallKind.NO_MODEL,
    "adelete_container_file": CallKind.NO_MODEL,
    "avideo_status": CallKind.NO_MODEL,
    "avideo_content": CallKind.NO_MODEL,
    "avideo_list": CallKind.NO_MODEL,
    "avideo_get_character": CallKind.NO_MODEL,
}


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
        if caller.agent_id is not None:
            return Permission.AGENT_KEY
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
        deployments = router.get_model_list(model_name=name) or []
        if not deployments:
            return None
        return self.candidate_group(name, deployments)

    def candidate_group(
        self, name: str, deployments: Sequence[Mapping[str, Any]]
    ) -> ModelGroup:
        return group_from_deployments(
            name,
            [self._billed_prices(deployment) for deployment in deployments],
            tuple(
                declared_data_class(deployment.get("model_info") or {})
                for deployment in deployments
            ),
        )

    def is_concrete(self, name: str) -> bool:
        router = self._router()
        if router is None or WILDCARD in name:
            return False
        return any(
            deployment.get("model_name") == name for deployment in router.model_list
        )

    def _billed_prices(self, deployment: Mapping[str, Any]) -> DeploymentPrices:
        router = self._router()
        if router is None:
            return UNPRICED
        info = router.get_deployment_model_info(
            model_id=str((deployment.get("model_info") or {}).get("id")),
            model_name=str((deployment.get("litellm_params") or {}).get("model")),
        )
        if info is None:
            return UNPRICED
        return (
            price(info.get("input_cost_per_token")),
            price(info.get("output_cost_per_token")),
        )

    def settings(self) -> GatewaySettings:
        return settings_from_general(self._general_settings())


def configured_groups(
    source: Path, model_list: Sequence[Mapping[str, Any]]
) -> dict[str, ModelGroup]:
    router: Final = _configured_router(source, model_list)
    state: Final = LiteLLMGatewayState(lambda: router, dict)
    by_group: dict[str, list[Mapping[str, Any]]] = {}
    for deployment in router.model_list:
        by_group.setdefault(str(deployment["model_name"]), []).append(deployment)
    return {
        name: state.candidate_group(name, deployments)
        for name, deployments in by_group.items()
    }


def _configured_router(source: Path, model_list: Sequence[Mapping[str, Any]]) -> Router:
    try:
        return _router_for(model_list)
    except (BadRequestError, ValueError) as error:
        raise DocumentError(_unloadable(source, model_list)) from error


def _router_for(model_list: Sequence[Mapping[str, Any]]) -> Router:
    with _litellm_errors_only():
        return Router(model_list=[_without_environment(entry) for entry in model_list])


@contextmanager
def _litellm_errors_only() -> Iterator[None]:
    loggers: Final = [logging.getLogger(name) for name in LITELLM_LOGGERS]
    levels: Final = [each.level for each in loggers]
    for each in loggers:
        each.setLevel(logging.ERROR)
    try:
        yield
    finally:
        for each, level in zip(loggers, levels, strict=True):
            each.setLevel(level)


def _without_environment(entry: Mapping[str, Any]) -> dict[str, Any]:
    copied: Final = copy.deepcopy(dict(entry))
    model_info: Final = copied.get("model_info")
    if isinstance(model_info, dict):
        model_info.pop(ENVIRONMENT_KEY, None)
    return copied


def _unloadable(source: Path, model_list: Sequence[Mapping[str, Any]]) -> str:
    for index, entry in enumerate(model_list, start=1):
        model = (entry.get("litellm_params") or {}).get("model")
        try:
            _router_for([entry])
        except (BadRequestError, ValueError):
            return (
                f"{source} model_list entry {index} names model {model!r}, which "
                "LiteLLM's router cannot load. Name a provider, such as openai/ or "
                "azure/, or set litellm_params.custom_llm_provider"
            )
    return f"{source} model_list cannot be loaded by LiteLLM's router"


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
        agent_id=auth.agent_id,
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


def request_team(data: Mapping[str, Any]) -> str | None:
    for container in METADATA_CONTAINERS:
        metadata = data.get(container)
        if not isinstance(metadata, dict):
            continue
        team_id = metadata.get(TEAM_ID_KEY)
        if isinstance(team_id, str):
            return team_id
    return None


def field_kind(field: Mapping[str, Any]) -> CallKind | None:
    written: Final = field.get(KIND_KEY)
    for kind in CallKind:
        if kind.value == written:
            return kind
    return None


def decided_route(field: Mapping[str, Any] | None) -> str | None:
    if field is None:
        return None
    route: Final = field.get(SERVED_ROUTE_KEY)
    if isinstance(route, str):
        return route
    return None


def call_kind(call_type: str) -> CallKind | None:
    return CALL_KINDS.get(call_type)


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
    container: Final = get_metadata_variable_name_from_kwargs(data)
    metadata = data.get(container)
    if not isinstance(metadata, dict):
        metadata = {}
        data[container] = metadata
    existing: Final = metadata.get(SPEND_LOGS_METADATA_KEY)
    if isinstance(existing, dict):
        metadata[SPEND_LOGS_METADATA_KEY] = {**existing, FIELD_KEY: field}
    else:
        metadata[SPEND_LOGS_METADATA_KEY] = {FIELD_KEY: field}


def read_field(data: Mapping[str, Any]) -> dict[str, Any] | None:
    for container in METADATA_CONTAINERS:
        metadata = data.get(container)
        if not isinstance(metadata, dict):
            continue
        spend_logs_metadata = metadata.get(SPEND_LOGS_METADATA_KEY)
        if not isinstance(spend_logs_metadata, dict):
            continue
        field = spend_logs_metadata.get(FIELD_KEY)
        if isinstance(field, dict):
            return field
    return None


def governed_field(
    kind: CallKind, team: TeamPolicy, decision: Decision, outcome: Outcome
) -> dict[str, Any]:
    field: dict[str, Any] = {
        KIND_KEY: kind.value,
        "requested_route": decision.route,
        "outcome": outcome_name(outcome),
        SERVED_ROUTE_KEY: served_route(outcome),
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


def admitted_field() -> dict[str, Any]:
    return {KIND_KEY: CallKind.NO_MODEL.value, "outcome": OUTCOME_ADMITTED}


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
    return sorted(key for key in CALLER_ROUTING_KEYS if key in data)


def apply(data: dict, outcome: Outcome, guardrails: Guardrails) -> dict:
    for key in NEUTRALISED_FALLBACK_KEYS:
        data[key] = []
    data[DISABLE_FALLBACKS_KEY] = True
    match outcome:
        case Served() | Rejected():
            return data
        case Degraded(target=target):
            data["model"] = target
            return guardrails.remerge(data)
        case _:
            assert_never(outcome)


def headers(
    data: dict, response: Any, litellm_call_info: dict[str, Any] | None
) -> dict[str, str] | None:
    field = read_field(data)
    if field is None or SERVED_ROUTE_KEY not in field:
        return None
    degraded_from = None
    if field.get("outcome") == OUTCOME_DEGRADED:
        degraded_from = field.get("requested_route")
    disclosed: dict[str, Any] = {
        HEADER_BUDGET_TIER: field.get("budget_tier"),
        HEADER_ROUTE_TIER: field.get("route_tier"),
        HEADER_DEGRADED_FROM: degraded_from,
        HEADER_DEGRADATION_REASON: field.get("degradation_reason"),
    }
    if response is not None:
        disclosed[HEADER_ROUTE_SERVED] = field[SERVED_ROUTE_KEY]
        disclosed[HEADER_DEPLOYMENT_ID] = (litellm_call_info or {}).get("model_id")
    result: Final = {
        name: str(value) for name, value in disclosed.items() if value is not None
    }
    if not result:
        return None
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
            | RefusalReason.MODEL_NOT_A_GROUP
            | RefusalReason.CALL_TYPE_NOT_GOVERNED
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
