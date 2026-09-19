"""Tier events, and the atomic claim that deduplicates them across replicas.

An event is a state transition, not a request. It carries the team, policy
epoch, window, tier, route and the two dollar figures, and nothing per-request.
A per-request dimension is what makes a metrics bill unbounded.

The claim is keyed on the policy epoch and budget window rather than on a TTL.
A reviewed policy change or window reset therefore re-arms every tier without
anyone guessing how long a warning should live. The key still carries an expiry
derived from the window so Redis does not accumulate claims forever, but
correctness comes from the key, not from the expiry.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx

from .runtime import spend_counter_redis

logger: Final = logging.getLogger("budget_policy")

WEBHOOK_TIMEOUT_SECONDS: Final = 1.0


class MetricSink(Protocol):
    """Where a state transition goes to be dashboarded and alerted on."""

    async def tier(self, event: TierEvent) -> None: ...

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
    """Why the decision came out that way.

    ``decision`` alone cannot separate a route with no declared target from one
    whose target failed validation or entitlement. Both admit the primary. The
    tier claim bounds this to one report per team per window, which is what
    keeps the rejection off the request path as a log line."""

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


@dataclass(frozen=True)
class IncidentEvent:
    """Enforcement itself is degraded. Page the platform, not the team."""

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
    """One winner per team, policy epoch, window and tier across every replica.

    Backed by the gateway's own coordination Redis through ``SET NX``. When no
    Redis is attached the store degrades to a per-replica set and says so, since
    a silently replica-local dedup would emit one event per replica per
    crossing. The claim is not exactly-once delivery: a process can stop after
    winning the claim and before an optional remote sink accepts the event.
    """

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
        """The gateway's spend-counter Redis.

        LiteLLM attaches Redis to this cache whenever a connection target is
        configured, and only attaches it to the auth cache behind an opt-in
        flag. Reusing it keeps the claim on the same Redis the spend counter
        already depends on, with no second connection to configure.
        """
        try:
            return spend_counter_redis()
        except Exception:  # noqa: BLE001 -- proxy import can fail outside its configured runtime
            return None


class EventEmitter:
    """A structured log line always, plus the webhook and metric sink when set.

    The log line is an operational record and always happens. It is not the
    durable audit store described in the target architecture. The webhook is
    how the lab observes a crossing. The metric sink is the platform-owned view
    a dashboard and an alert read, and it carries no content.
    """

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
        """Attempt independent remote sinks concurrently without breaking inference."""
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
