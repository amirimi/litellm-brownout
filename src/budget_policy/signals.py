from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

import httpx

from .runtime import spend_counter_redis

logger: Final = logging.getLogger("budget_policy")

WEBHOOK_TIMEOUT_SECONDS: Final = 1.0


class MetricSink(Protocol):
    async def tier(self, event: TierEvent) -> None: ...

    async def fallback_rejected(self, event: FallbackRejectedEvent) -> None: ...

    async def route_degraded(self, event: RouteDegradedEvent) -> None: ...

    async def incident(self, event: IncidentEvent) -> None: ...


@dataclass(frozen=True)
class TierEvent:
    team_id: str
    policy_epoch: str
    window: str
    tier: int
    route: str
    served_route: str
    decision: str
    reason: str

    spend_usd: float
    envelope_usd: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "event": "budget_tier_crossed",
            "owner": "team",
            "team_id": self.team_id,
            "policy_epoch": self.policy_epoch,
            "window": self.window,
            "tier": self.tier,
            "route": self.route,
            "served_route": self.served_route,
            "decision": self.decision,
            "reason": self.reason,
            "spend_usd": round(self.spend_usd, 6),
            "envelope_usd": self.envelope_usd,
        }


class Rejection(StrEnum):
    OUTSIDE_CALLER_ALLOWLIST = "outside_caller_allowlist"
    ENTITLEMENT_CHECK_UNAVAILABLE = "entitlement_check_unavailable"


@dataclass(frozen=True)
class RouteDegradedEvent:
    team_id: str
    policy_epoch: str
    window: str
    route: str
    served_route: str
    tier: int
    spend_usd: float
    envelope_usd: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "event": "budget_route_degraded",
            "owner": "team",
            "team_id": self.team_id,
            "policy_epoch": self.policy_epoch,
            "window": self.window,
            "route": self.route,
            "served_route": self.served_route,
            "tier": self.tier,
            "spend_usd": round(self.spend_usd, 6),
            "envelope_usd": self.envelope_usd,
        }


@dataclass(frozen=True)
class FallbackRejectedEvent:
    team_id: str
    policy_epoch: str
    window: str
    route: str
    target: str
    rejection: Rejection
    tier: int
    spend_usd: float
    envelope_usd: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "event": "budget_fallback_rejected",
            "owner": "team",
            "team_id": self.team_id,
            "policy_epoch": self.policy_epoch,
            "window": self.window,
            "route": self.route,
            "target": self.target,
            "rejection": self.rejection.value,
            "tier": self.tier,
            "spend_usd": round(self.spend_usd, 6),
            "envelope_usd": self.envelope_usd,
        }


@dataclass(frozen=True)
class IncidentEvent:
    reason: str
    window: str
    detail: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "event": "budget_policy_incident",
            "owner": "platform",
            "reason": self.reason,
            "window": self.window,
            **self.detail,
        }


class TierSignalStore:
    def __init__(self, namespace: str = "budget_policy") -> None:
        self._namespace: Final = namespace
        self._local: Final[set[str]] = set()
        self._warned_local_only = False

    async def claim(self, key: str, ttl_seconds: int) -> bool:
        redis_cache = self._redis_cache()
        if redis_cache is None:
            return self._claim_locally(key)
        try:
            claimed = await redis_cache.async_set_cache(
                key=self._qualified(key),
                value=1,
                nx=True,
                ttl=ttl_seconds,
            )
        except Exception:
            logger.exception(
                "Tier signal claim failed for %s, falling back to replica-local dedup",
                key,
            )
            return self._claim_locally(key)
        return bool(claimed)

    def _claim_locally(self, key: str) -> bool:
        if not self._warned_local_only:
            logger.warning(
                "Tier signal store has no Redis attached. Deduplication is replica-local, "
                "so a multi-replica gateway will emit one event per replica per crossing"
            )
            self._warned_local_only = True
        if key in self._local:
            return False
        self._local.add(key)
        return True

    def _qualified(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    @staticmethod
    def _redis_cache() -> Any | None:
        try:
            return spend_counter_redis()
        except Exception:  # noqa: BLE001
            return None


class EventEmitter:
    def __init__(
        self, webhook_url: str | None, metric_sink: MetricSink | None = None
    ) -> None:
        self._webhook_url: Final = webhook_url
        self._metric_sink: Final = metric_sink

    async def emit_tier(self, event: TierEvent) -> None:
        payload = event.as_payload()
        logger.warning("budget_policy %s", json.dumps(payload, sort_keys=True))
        metric_delivery = None
        if self._metric_sink is not None:
            metric_delivery = self._metric_sink.tier(event)
        await self._deliver(payload, metric_delivery)

    async def emit_route_degraded(self, event: RouteDegradedEvent) -> None:
        payload = event.as_payload()
        logger.warning("budget_policy %s", json.dumps(payload, sort_keys=True))
        metric_delivery = None
        if self._metric_sink is not None:
            metric_delivery = self._metric_sink.route_degraded(event)
        await self._deliver(payload, metric_delivery)

    async def emit_fallback_rejected(self, event: FallbackRejectedEvent) -> None:
        payload = event.as_payload()
        logger.warning("budget_policy %s", json.dumps(payload, sort_keys=True))
        metric_delivery = None
        if self._metric_sink is not None:
            metric_delivery = self._metric_sink.fallback_rejected(event)
        await self._deliver(payload, metric_delivery)

    async def emit_incident(self, event: IncidentEvent) -> None:
        payload = event.as_payload()
        logger.error("budget_policy %s", json.dumps(payload, sort_keys=True))
        metric_delivery = None
        if self._metric_sink is not None:
            metric_delivery = self._metric_sink.incident(event)
        await self._deliver(payload, metric_delivery)

    async def _deliver(
        self,
        payload: dict[str, Any],
        metric_delivery: Awaitable[None] | None,
    ) -> None:
        deliveries: list[Awaitable[None]] = [self._post(payload)]
        if metric_delivery is not None:
            deliveries.append(metric_delivery)
        results = await asyncio.gather(*deliveries, return_exceptions=True)
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                logger.error(
                    "Budget event remote sink failed",
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def _post(self, payload: dict[str, Any]) -> None:
        if not self._webhook_url:
            return
        try:
            async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
                response = await client.post(self._webhook_url, json=payload)
                response.raise_for_status()
        except Exception:
            logger.exception("Tier event delivery to %s failed", self._webhook_url)
