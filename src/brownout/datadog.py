from __future__ import annotations

import logging
import time
from typing import Any, Final, assert_never

import httpx

from .events import (
    Event,
    FallbackRejectedEvent,
    IncidentEvent,
    RouteDegradedEvent,
    TierEvent,
    TierScope,
)

logger: Final = logging.getLogger("brownout")

SUBMIT_TIMEOUT_SECONDS: Final = 1.0
COUNT: Final = 1
GAUGE: Final = 3

METRIC_TIER_CROSSED: Final = "brownout.tier_crossed"
METRIC_UTILISATION: Final = "brownout.envelope_utilisation"
METRIC_ROUTE_DEGRADED: Final = "brownout.route_degraded"
METRIC_FALLBACK_REJECTED: Final = "brownout.fallback_rejected"
METRIC_INCIDENT: Final = "brownout.incident"


class DatadogSink:
    def __init__(self, api_key: str, site: str, env: str) -> None:
        self._api_key: Final = api_key
        self._url: Final = f"https://api.{site}/api/v2/series"
        self._base_tags: Final = (f"env:{env}", "service:brownout")

    async def submit(self, event: Event) -> None:
        await self._post(self._series_for(event))

    def _series_for(self, event: Event) -> list[dict[str, Any]]:
        tags: tuple[str, ...]
        match event:
            case TierEvent():
                return self._tier_series(event)
            case RouteDegradedEvent():
                tags = self._base_tags + (
                    f"team:{event.team_id}",
                    f"route:{event.route}",
                    f"served_route:{event.served_route}",
                )
                return [_series(METRIC_ROUTE_DEGRADED, COUNT, 1.0, tags)]
            case FallbackRejectedEvent():
                tags = self._base_tags + (
                    f"team:{event.team_id}",
                    f"route:{event.route}",
                    f"rejection:{event.rejection.value}",
                )
                return [_series(METRIC_FALLBACK_REJECTED, COUNT, 1.0, tags)]
            case IncidentEvent():
                tags = self._base_tags + (f"reason:{event.reason}",)
                team_id = event.detail.get("team_id")
                if team_id is not None:
                    tags += (f"team:{team_id}",)
                return [_series(METRIC_INCIDENT, COUNT, 1.0, tags)]
            case _:
                assert_never(event)

    def _tier_series(self, event: TierEvent) -> list[dict[str, Any]]:
        tags = self._base_tags + (
            f"team:{event.team_id}",
            f"tier:{event.tier}",
            f"scope:{event.scope.value}",
            f"outcome:{event.outcome}",
        )
        match event.scope:
            case TierScope.TEAM:
                utilisation = event.spend_usd / event.envelope_usd
                return [
                    _series(METRIC_TIER_CROSSED, COUNT, 1.0, tags),
                    _series(
                        METRIC_UTILISATION,
                        GAUGE,
                        utilisation,
                        self._base_tags + (f"team:{event.team_id}",),
                    ),
                ]
            case TierScope.ROUTE:
                return [
                    _series(
                        METRIC_TIER_CROSSED,
                        COUNT,
                        1.0,
                        tags + (f"route:{event.route}",),
                    )
                ]
            case _:
                assert_never(event.scope)

    async def _post(self, series: list[dict[str, Any]]) -> None:
        try:
            async with httpx.AsyncClient(timeout=SUBMIT_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    self._url,
                    json={"series": series},
                    headers={
                        "DD-API-KEY": self._api_key,
                        "Content-Type": "application/json",
                    },
                )
            if response.status_code >= 300:
                logger.error(
                    "Datadog metric submission rejected with status %s: %s",
                    response.status_code,
                    response.text[:200],
                )
        except Exception:
            logger.exception("Datadog metric submission to %s failed", self._url)


def _series(
    metric: str, metric_type: int, value: float, tags: tuple[str, ...]
) -> dict[str, Any]:
    return {
        "metric": metric,
        "type": metric_type,
        "points": [{"timestamp": int(time.time()), "value": value}],
        "tags": list(tags),
    }
