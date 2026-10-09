from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol, assert_never

import httpx

from .policy import Rejection

logger: Final = logging.getLogger("brownout")

WEBHOOK_TIMEOUT_SECONDS: Final = 1.0


class TierScope(StrEnum):
    TEAM = "team"
    ROUTE = "route"


class Owner(StrEnum):
    TEAM = "team"
    PLATFORM = "platform"


@dataclass(frozen=True)
class TierEvent:
    team_id: str
    policy_epoch: str
    window: str
    scope: TierScope
    route: str
    tier: int
    served_route: str
    outcome: str
    spend_usd: float
    envelope_usd: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "event": "brownout_tier_crossed",
            "owner": Owner.TEAM.value,
            "team_id": self.team_id,
            "policy_epoch": self.policy_epoch,
            "window": self.window,
            "scope": self.scope.value,
            "route": self.route,
            "tier": self.tier,
            "served_route": self.served_route,
            "outcome": self.outcome,
            "spend_usd": round(self.spend_usd, 6),
            "envelope_usd": self.envelope_usd,
        }


@dataclass(frozen=True)
class RouteDegradedEvent:
    team_id: str
    policy_epoch: str
    window: str
    route: str
    served_route: str
    degrade_at: int
    spend_usd: float
    envelope_usd: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "event": "brownout_route_degraded",
            "owner": Owner.TEAM.value,
            "team_id": self.team_id,
            "policy_epoch": self.policy_epoch,
            "window": self.window,
            "route": self.route,
            "served_route": self.served_route,
            "degrade_at": self.degrade_at,
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
    degrade_at: int
    spend_usd: float
    envelope_usd: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "event": "brownout_fallback_rejected",
            "owner": Owner.TEAM.value,
            "team_id": self.team_id,
            "policy_epoch": self.policy_epoch,
            "window": self.window,
            "route": self.route,
            "target": self.target,
            "rejection": self.rejection.value,
            "degrade_at": self.degrade_at,
            "spend_usd": round(self.spend_usd, 6),
            "envelope_usd": self.envelope_usd,
        }


@dataclass(frozen=True)
class IncidentEvent:
    reason: str
    window: str
    detail: Mapping[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "event": "brownout_incident",
            "owner": Owner.PLATFORM.value,
            "reason": self.reason,
            "window": self.window,
            **self.detail,
        }


Event = TierEvent | RouteDegradedEvent | FallbackRejectedEvent | IncidentEvent


class MetricSink(Protocol):
    async def submit(self, event: Event) -> None: ...


class EventEmitter:
    def __init__(self, webhook_url: str | None, metric_sink: MetricSink | None) -> None:
        self._webhook_url: Final = webhook_url
        self._metric_sink: Final = metric_sink

    async def emit(self, event: Event) -> None:
        payload: Final = event.as_payload()
        _log(event, json.dumps(payload, sort_keys=True))
        deliveries: list[Awaitable[None]] = [self._post(payload)]
        if self._metric_sink is not None:
            deliveries.append(self._metric_sink.submit(event))
        results: Final = await asyncio.gather(*deliveries, return_exceptions=True)
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                logger.error(
                    "Brownout event remote sink failed",
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
            logger.exception("Brownout event delivery to %s failed", self._webhook_url)


def _log(event: Event, line: str) -> None:
    match event:
        case IncidentEvent():
            logger.error("brownout %s", line)
        case TierEvent() | RouteDegradedEvent() | FallbackRejectedEvent():
            logger.warning("brownout %s", line)
        case _:
            assert_never(event)
