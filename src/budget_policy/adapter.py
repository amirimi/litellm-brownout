from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from typing import Any, Final, Literal, assert_never

from fastapi import HTTPException, status
from litellm.caching.dual_cache import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.duration_parser import duration_in_seconds
from litellm.proxy._types import ProxyException, UserAPIKeyAuth
from litellm.proxy.auth.auth_checks import (
    _can_object_call_model,
    _resolve_key_models_for_auth_check,
)
from litellm.proxy.common_utils.timezone_utils import get_budget_reset_time
from litellm.proxy.utils import _check_and_merge_model_level_guardrails

from .catalog import PriceCatalog, UnpricedModel
from .manifest import Manifest, RoutePolicy, TeamPolicy
from .metrics import DatadogMetricSink
from .policy import Action, Decision, PolicyRequest, Reason, decide
from .runtime import router, spend_reader
from .signals import (
    EventEmitter,
    FallbackRejectedEvent,
    IncidentEvent,
    Rejection,
    RouteDegradedEvent,
    TierEvent,
    TierSignalStore,
)

logger: Final = logging.getLogger("budget_policy")

CHAT_COMPLETION_CALL_TYPES: Final = frozenset({"completion", "acompletion"})
DISCLOSURE_KEY: Final = "_budget_policy"
RESERVED_METADATA_PREFIX: Final = "x-platform-"
CALLER_ROUTING_KEYS: Final = ("fallbacks", "mock_testing_fallbacks")
METADATA_CONTAINERS: Final = ("metadata", "litellm_metadata")
_CEILING_TOLERANCE_USD: Final = 1e-9

UNGOVERNED_WINDOW_DURATION: Final = "7d"

_ALREADY_SIGNALLED_IN_THE_GATEWAY: Final[frozenset[int]] = frozenset()

HEADER_ROUTE_SERVED: Final = "x-platform-route-served"
HEADER_DEGRADED_FROM: Final = "x-platform-degraded-from"
HEADER_DEGRADATION_REASON: Final = "x-platform-degradation-reason"
HEADER_BUDGET_TIER: Final = "x-platform-budget-tier"
HEADER_DEPLOYMENT_ID: Final = "x-platform-deployment-id"

DISCLOSED_REASONS: Final = frozenset(
    {Reason.DEGRADE_TIER_REACHED.value, Reason.FALLBACK_REJECTED.value}
)


class Entitlement(StrEnum):
    PERMITTED = "permitted"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class FallbackRejected:
    candidate: str
    reason: Rejection


def _team_counter_key(team_id: str) -> str:
    return f"spend:team:{team_id}"


class TeamSpendReader:
    async def read(
        self, team_id: str, fallback_spend: float, max_budget: float
    ) -> float:
        return float(
            await spend_reader()(
                counter_key=_team_counter_key(team_id),
                fallback_spend=fallback_spend,
                max_budget=max_budget,
            )
        )


class BudgetPolicyHandler(CustomLogger):
    def __init__(
        self,
        catalog: PriceCatalog | None = None,
        manifest: Manifest | None = None,
        store: TierSignalStore | None = None,
        emitter: EventEmitter | None = None,
        spend_reader: TeamSpendReader | None = None,
    ) -> None:
        super().__init__()
        if catalog is None:
            catalog = PriceCatalog.from_file(_prices_path())
        if manifest is None:
            manifest = Manifest.from_file(_manifest_path())
        if store is None:
            store = TierSignalStore()
        if emitter is None:
            emitter = EventEmitter(_webhook_url(), _metric_sink())
        if spend_reader is None:
            spend_reader = TeamSpendReader()
        _require_declared_fallbacks(manifest, catalog)

        self._catalog: Final = catalog
        self._manifest: Final = manifest
        self._store: Final = store
        self._emitter: Final = emitter
        self._spend_reader: Final = spend_reader
        logger.info(
            "budget_policy loaded: catalog=%s manifest=%s teams=%s",
            self._catalog.version,
            self._manifest.version,
            ",".join(self._manifest.team_ids),
        )

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: str,
    ) -> dict:
        if call_type not in CHAT_COMPLETION_CALL_TYPES:
            return data
        _clear_disclosure(data)
        _refuse_caller_routing(data)

        team = self._manifest.team_policy(user_api_key_dict.team_id)
        if team is None:
            await self._report_ungoverned_team(user_api_key_dict.team_id)
            raise _ungoverned_team(user_api_key_dict.team_id)

        route = str(data.get("model") or "")
        route_policy = team.route_policy(route)
        window_id = _window_id(team.budget_duration)

        await self._require_priced(route=route, team=team, window_id=window_id)
        await self._require_budget_ceiling(
            auth=user_api_key_dict, team=team, window_id=window_id
        )

        projected_spend: Final = await self._projected_spend(user_api_key_dict, team)
        selection: Final = await self._selectable_fallback(
            user_api_key_dict, route, route_policy, team, window_id
        )
        decision = decide(
            PolicyRequest(
                team=team,
                route=route_policy,
                projected_spend_usd=projected_spend,
                already_signalled=_ALREADY_SIGNALLED_IN_THE_GATEWAY,
                selectable_fallback=_selectable_target(selection),
            )
        )

        if decision.action is Action.BLOCK:
            await self._publish(
                team=team,
                window_id=window_id,
                route=route,
                decision=decision,
                selection=selection,
            )
            raise _enforcement_unavailable(team=team, route=route)

        applied: Final = _apply(data, decision, team)
        await self._publish(
            team=team,
            window_id=window_id,
            route=route,
            decision=decision,
            selection=selection,
        )
        return applied

    async def async_post_call_response_headers_hook(
        self,
        data: dict,
        user_api_key_dict: UserAPIKeyAuth,
        response: Any,
        request_headers: dict[str, str] | None = None,
        litellm_call_info: dict[str, Any] | None = None,
    ) -> dict[str, str] | None:
        disclosure = (data.get("metadata") or {}).get(DISCLOSURE_KEY)
        if not isinstance(disclosure, dict):
            return None

        headers = {HEADER_ROUTE_SERVED: str(disclosure["served_route"])}
        deployment_id = (litellm_call_info or {}).get("model_id")
        if deployment_id is not None:
            headers[HEADER_DEPLOYMENT_ID] = str(deployment_id)
        if disclosure.get("tier") is not None:
            headers[HEADER_BUDGET_TIER] = str(disclosure["tier"])
        if disclosure.get("degraded_from") is not None:
            headers[HEADER_DEGRADED_FROM] = str(disclosure["degraded_from"])
        if str(disclosure["reason"]) in DISCLOSED_REASONS:
            headers[HEADER_DEGRADATION_REASON] = str(disclosure["reason"])
        return headers

    async def _require_priced(
        self, route: str, team: TeamPolicy, window_id: str
    ) -> None:
        try:
            self._catalog.price(route)
        except UnpricedModel:
            served: Final = route in _served_model_groups(team.team_id)
            if served:
                dedup = f"incident:unpriced_model:group:{route}:{window_id}"
            else:
                dedup = f"incident:unpriced_model:team:{team.team_id}:{window_id}"
            await self._incident(
                reason="unpriced_model",
                window_id=window_id,
                detail={
                    "model": route,
                    "model_group_served": served,
                    "team_id": team.team_id,
                    "catalog_version": self._catalog.version,
                },
                dedup=dedup,
                ttl_seconds=_window_ttl(team.budget_duration),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "budget_policy_unavailable",
                    "reason": "unpriced_model",
                    "model": route,
                    "message": (
                        f"Model {route!r} has no committed price in catalog version "
                        f"{self._catalog.version}, so its spend cannot be bounded. The "
                        "request was refused rather than admitted at an unknown cost."
                    ),
                },
            ) from None

    async def _report_ungoverned_team(self, team_id: str | None) -> None:
        window_id: Final = _window_id(UNGOVERNED_WINDOW_DURATION)
        incident_team_id = team_id
        if incident_team_id is None:
            incident_team_id = "<missing>"
        await self._incident(
            reason="team_not_in_manifest",
            window_id=window_id,
            detail={
                "team_id": incident_team_id,
                "manifest_version": self._manifest.version,
            },
            dedup=f"incident:team_not_in_manifest:{incident_team_id}:{window_id}",
            ttl_seconds=_window_ttl(UNGOVERNED_WINDOW_DURATION),
        )

    async def _require_budget_ceiling(
        self,
        auth: UserAPIKeyAuth,
        team: TeamPolicy,
        window_id: str,
    ) -> None:
        ceiling = auth.team_max_budget
        if ceiling is not None and (
            abs(float(ceiling) - team.envelope_usd) <= _CEILING_TOLERANCE_USD
        ):
            return
        actual_ceiling = None
        if ceiling is not None:
            actual_ceiling = float(ceiling)
        await self._incident(
            reason="budget_ceiling_drift",
            window_id=window_id,
            detail={
                "team_id": team.team_id,
                "envelope_usd": team.envelope_usd,
                "team_max_budget_usd": actual_ceiling,
                "manifest_version": self._manifest.version,
            },
            dedup=f"incident:budget_ceiling_drift:{team.team_id}:{window_id}:{actual_ceiling}",
            ttl_seconds=_window_ttl(team.budget_duration),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "budget_policy_unavailable",
                "reason": "budget_ceiling_drift",
                "team_id": team.team_id,
                "message": (
                    "The runtime team budget no longer equals the owner-approved "
                    "envelope, so no tier below it means what it claims."
                ),
            },
        )

    async def _projected_spend(
        self, auth: UserAPIKeyAuth, team: TeamPolicy
    ) -> float | None:
        try:
            counter = await self._spend_reader.read(
                team_id=team.team_id,
                fallback_spend=float(auth.team_spend or 0.0),
                max_budget=(
                    float(auth.team_max_budget)
                    if auth.team_max_budget is not None
                    else team.envelope_usd
                ),
            )
        except Exception:
            logger.exception("Team spend counter unreadable for team=%s", team.team_id)
            return None
        return max(0.0, counter - _own_hold_on_team_counter(auth, team.team_id))

    async def _selectable_fallback(
        self,
        auth: UserAPIKeyAuth,
        route: str,
        route_policy: RoutePolicy,
        team: TeamPolicy,
        window_id: str,
    ) -> str | FallbackRejected | None:
        selected: Final = route_policy.fallback
        if selected is None:
            return None

        entitlement = _caller_may_call(auth, selected)
        match entitlement:
            case Entitlement.PERMITTED:
                return selected
            case Entitlement.DENIED:
                logger.debug(
                    "Rejected degradation target %s for route %s on team %s: outside the caller allowlist",
                    selected,
                    route,
                    team.team_id,
                )
                return FallbackRejected(selected, Rejection.OUTSIDE_CALLER_ALLOWLIST)
            case Entitlement.UNAVAILABLE:
                await self._incident(
                    reason="entitlement_check_unavailable",
                    window_id=window_id,
                    detail={
                        "team_id": team.team_id,
                        "route": route,
                        "target": selected,
                    },
                    dedup=f"incident:entitlement_check_unavailable:{team.team_id}:{window_id}",
                    ttl_seconds=_window_ttl(team.budget_duration),
                )
                return FallbackRejected(
                    selected, Rejection.ENTITLEMENT_CHECK_UNAVAILABLE
                )
            case _:
                assert_never(entitlement)

    async def _publish(
        self,
        team: TeamPolicy,
        window_id: str,
        route: str,
        decision: Decision,
        selection: str | FallbackRejected | None,
    ) -> None:
        if decision.reason is Reason.COUNTER_UNAVAILABLE:
            await self._report_unreadable_counter(
                team=team, window_id=window_id, route=route, decision=decision
            )
            return
        deliveries: list[Awaitable[None]] = [
            self._emitter.emit_tier(event)
            for event in await self._claim_tiers(
                team=team, window_id=window_id, route=route, decision=decision
            )
        ]
        rejection: Final = await self._claim_rejection(
            team=team,
            window_id=window_id,
            route=route,
            decision=decision,
            selection=selection,
        )
        if rejection is not None:
            deliveries.append(self._emitter.emit_fallback_rejected(rejection))
        degradation: Final = await self._claim_degradation(
            team=team, window_id=window_id, route=route, decision=decision
        )
        if degradation is not None:
            deliveries.append(self._emitter.emit_route_degraded(degradation))
        await asyncio.gather(*deliveries)

    async def _report_unreadable_counter(
        self, team: TeamPolicy, window_id: str, route: str, decision: Decision
    ) -> None:
        await self._incident(
            reason="spend_counter_unreadable",
            window_id=window_id,
            detail={
                "team_id": team.team_id,
                "route": route,
                "posture": decision.action.value,
            },
            dedup=f"incident:spend_counter_unreadable:{team.team_id}:{window_id}",
            ttl_seconds=_window_ttl(team.budget_duration),
        )

    async def _claim_tiers(
        self, team: TeamPolicy, window_id: str, route: str, decision: Decision
    ) -> list[TierEvent]:
        assert decision.utilisation is not None
        ttl_seconds: Final = _window_ttl(team.budget_duration)
        claimed: list[TierEvent] = []
        for tier in decision.tiers_crossed:
            claim_key = f"tier:{team.team_id}:{team.policy_epoch}:{window_id}:{tier}"
            if not await self._store.claim(claim_key, ttl_seconds):
                continue
            claimed.append(
                TierEvent(
                    team_id=team.team_id,
                    policy_epoch=team.policy_epoch,
                    window=window_id,
                    tier=tier,
                    route=route,
                    served_route=decision.served_route,
                    decision=decision.action.value,
                    reason=decision.reason.value,
                    spend_usd=decision.utilisation * team.envelope_usd,
                    envelope_usd=team.envelope_usd,
                )
            )
        return claimed

    async def _claim_degradation(
        self, team: TeamPolicy, window_id: str, route: str, decision: Decision
    ) -> RouteDegradedEvent | None:
        if decision.reason is not Reason.DEGRADE_TIER_REACHED:
            return None
        assert decision.tier is not None and decision.utilisation is not None
        claim_key: Final = (
            f"degraded:{team.team_id}:{team.policy_epoch}:{window_id}:{route}"
        )
        if not await self._store.claim(claim_key, _window_ttl(team.budget_duration)):
            return None
        return RouteDegradedEvent(
            team_id=team.team_id,
            policy_epoch=team.policy_epoch,
            window=window_id,
            route=route,
            served_route=decision.served_route,
            tier=decision.tier,
            spend_usd=decision.utilisation * team.envelope_usd,
            envelope_usd=team.envelope_usd,
        )

    async def _claim_rejection(
        self,
        team: TeamPolicy,
        window_id: str,
        route: str,
        decision: Decision,
        selection: str | FallbackRejected | None,
    ) -> FallbackRejectedEvent | None:
        if decision.reason is not Reason.FALLBACK_REJECTED:
            return None
        assert isinstance(selection, FallbackRejected), (
            f"route {route!r} reported fallback_rejected without a rejected target"
        )
        assert decision.tier is not None and decision.utilisation is not None
        claim_key: Final = (
            f"fallback_rejected:{team.team_id}:{team.policy_epoch}:{window_id}:"
            f"{route}:{selection.reason.value}"
        )
        if not await self._store.claim(claim_key, _window_ttl(team.budget_duration)):
            return None
        return FallbackRejectedEvent(
            team_id=team.team_id,
            policy_epoch=team.policy_epoch,
            window=window_id,
            route=route,
            target=selection.candidate,
            rejection=selection.reason,
            tier=decision.tier,
            spend_usd=decision.utilisation * team.envelope_usd,
            envelope_usd=team.envelope_usd,
        )

    async def _incident(
        self,
        reason: str,
        window_id: str,
        detail: dict[str, Any],
        dedup: str,
        ttl_seconds: int,
    ) -> None:
        if not await self._store.claim(dedup, ttl_seconds):
            return
        await self._emitter.emit_incident(
            IncidentEvent(reason=reason, window=window_id, detail=detail)
        )


def _apply(data: dict, decision: Decision, team: TeamPolicy) -> dict:
    if decision.action is Action.DEGRADE:
        data["model"] = decision.served_route
        data = _remerge_guardrails(data)

    metadata = data.setdefault("metadata", {})
    metadata[DISCLOSURE_KEY] = {
        "served_route": decision.served_route,
        "degraded_from": decision.degraded_from,
        "tier": decision.tier,
        "reason": decision.reason.value,
        "team_id": team.team_id,
    }
    return data


def _served_model_groups(team_id: str) -> frozenset[str]:
    gateway_router: Final = router()
    if gateway_router is None:
        return frozenset()
    return frozenset(gateway_router.get_model_names(team_id=team_id))


def _require_declared_fallbacks(manifest: Manifest, catalog: PriceCatalog) -> None:
    for team_id in manifest.team_ids:
        team = manifest.team_policy(team_id)
        assert team is not None
        for route, route_policy in team.routes.items():
            if route_policy.fallback is not None:
                catalog.require_fallback(route, route_policy.fallback)


def _selectable_target(selection: str | FallbackRejected | None) -> str | None:
    if isinstance(selection, FallbackRejected):
        return None
    return selection


def _own_hold_on_team_counter(auth: UserAPIKeyAuth, team_id: str) -> float:
    reservation: Final = auth.budget_reservation
    if reservation is None:
        return 0.0
    counter_key: Final = _team_counter_key(team_id)
    return sum(
        float(entry.get("reserved_cost") or 0.0)
        + float(entry.get("applied_adjustment") or 0.0)
        for entry in reservation.get("entries") or []
        if isinstance(entry, dict) and entry.get("counter_key") == counter_key
    )


def _caller_may_call(auth: UserAPIKeyAuth, model: str) -> Entitlement:
    scopes: Final = (
        _scope_may_call(
            auth,
            model,
            models=_resolve_key_models_for_auth_check(auth),
            object_type="key",
        ),
        _scope_may_call(
            auth, model, models=list(auth.team_models or []), object_type="team"
        ),
    )
    if Entitlement.UNAVAILABLE in scopes:
        return Entitlement.UNAVAILABLE
    if Entitlement.DENIED in scopes:
        return Entitlement.DENIED
    return Entitlement.PERMITTED


def _scope_may_call(
    auth: UserAPIKeyAuth,
    model: str,
    models: list[str],
    object_type: Literal["key", "team"],
) -> Entitlement:
    try:
        _can_object_call_model(
            model=model,
            llm_router=router(),
            models=models,
            team_id=auth.team_id,
            object_type=object_type,
        )
    except ProxyException:
        return Entitlement.DENIED
    except Exception:
        logger.exception(
            "Entitlement check for model %s in %s scope did not complete",
            model,
            object_type,
        )
        return Entitlement.UNAVAILABLE
    return Entitlement.PERMITTED


def _remerge_guardrails(data: dict) -> dict:
    return _check_and_merge_model_level_guardrails(
        data=data,
        llm_router=router(),
        trust_client_model_info=False,
    )


def _clear_disclosure(data: dict) -> None:
    for container in METADATA_CONTAINERS:
        metadata = data.get(container)
        if isinstance(metadata, dict):
            metadata.pop(DISCLOSURE_KEY, None)


def _refuse_caller_routing(data: dict) -> None:
    refused: Final = _caller_routing_keys(data)
    if not refused:
        return
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "error": "caller_routing_not_permitted",
            "reason": "caller_routing_not_permitted",
            "refused": refused,
            "message": (
                "This route is governed by a budget policy that selects and validates its own "
                "degradation target. Caller-supplied routing and reserved platform metadata are "
                "refused rather than ignored, so a request is never served as though it had been "
                "honoured when it was not."
            ),
        },
    )


def _caller_routing_keys(data: dict) -> list[str]:
    found: list[str] = [key for key in CALLER_ROUTING_KEYS if key in data]
    for container in METADATA_CONTAINERS:
        metadata = data.get(container)
        if not isinstance(metadata, dict):
            continue
        for key in metadata:
            if _is_reserved_metadata_key(str(key)):
                found.append(f"{container}.{key}")
    return sorted(found)


def _is_reserved_metadata_key(key: str) -> bool:
    if key.lower().startswith(RESERVED_METADATA_PREFIX):
        return True
    return key == "degraded_from"


def _enforcement_unavailable(team: TeamPolicy, route: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "budget_policy_unavailable",
            "reason": "spend_counter_unreadable",
            "team_id": team.team_id,
            "message": (
                f"The spend counter for team {team.team_id!r} could not be read, and "
                f"route {route!r} declares a fail-closed posture. Retry shortly."
            ),
        },
    )


def _ungoverned_team(team_id: str | None) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "budget_policy_unavailable",
            "reason": "team_not_in_manifest",
            "team_id": team_id,
            "message": (
                "This key's team declares no budget envelope or price policy, so its "
                "spend cannot be attributed or bounded."
            ),
        },
    )


def _window_id(budget_duration: str) -> str:
    return get_budget_reset_time(budget_duration).astimezone(UTC).isoformat()


def _window_ttl(budget_duration: str) -> int:
    return duration_in_seconds(budget_duration) * 2


def _prices_path() -> str:
    return os.getenv("BUDGET_POLICY_PRICES", "config/prices.yaml")


def _manifest_path() -> str:
    return os.getenv("BUDGET_POLICY_MANIFEST", "config/manifest.yaml")


def _webhook_url() -> str | None:
    return os.getenv("BUDGET_POLICY_WEBHOOK_URL")


def _metric_sink() -> DatadogMetricSink | None:
    api_key = os.getenv("DD_API_KEY")
    if not api_key:
        return None
    return DatadogMetricSink(
        api_key=api_key,
        site=os.getenv("DD_SITE", "datadoghq.com"),
        env=os.getenv("DD_ENV", "unknown"),
    )


handler: Final = BudgetPolicyHandler()
