from __future__ import annotations

import logging
import time
from typing import Any, Final

import httpx

from .signals import (
    FallbackRejectedEvent,
    IncidentEvent,
    RouteDegradedEvent,
    TierEvent,
)

logger: Final = logging.getLogger("budget_policy")

SUBMIT_TIMEOUT_SECONDS: Final = 1.0
COUNT: Final = 1
GAUGE: Final = 3

METRIC_TIER_CROSSED: Final = "budget_policy.tier_crossed"
METRIC_UTILISATION: Final = "budget_policy.envelope_utilisation"
METRIC_ROUTE_DEGRADED: Final = "budget_policy.route_degraded"
METRIC_FALLBACK_REJECTED: Final = "budget_policy.fallback_rejected"
METRIC_INCIDENT: Final = "budget_policy.incident"


class DatadogMetricSink:
    def __init__(self, api_key: str, site: str, env: str) -> None:
        self._api_key: Final = api_key
        self._url: Final = f"https://api.{site}/api/v2/series"
        self._base_tags: Final = (f"env:{env}", "service:budget-policy")

    async def tier(self, event: TierEvent) -> None:
        tags: tuple[str, ...] = self._base_tags + (
            f"team:{event.team_id}",
            f"tier:{event.tier}",
            f"decision:{event.decision}",
            f"reason:{event.reason}",
            f"route:{event.route}",
        )
        if event.envelope_usd:
            utilisation = event.spend_usd / event.envelope_usd
        else:
            utilisation = 0.0
        await self._submit(
            [
                _series(METRIC_TIER_CROSSED, COUNT, 1.0, tags),
                _series(
                    METRIC_UTILISATION,
                    GAUGE,
                    utilisation,
                    self._base_tags + (f"team:{event.team_id}",),
                ),
            ]
        )

    async def route_degraded(self, event: RouteDegradedEvent) -> None:
        tags: Final = self._base_tags + (
            f"team:{event.team_id}",
            f"route:{event.route}",
            f"served_route:{event.served_route}",
        )
        await self._submit([_series(METRIC_ROUTE_DEGRADED, COUNT, 1.0, tags)])

    async def fallback_rejected(self, event: FallbackRejectedEvent) -> None:
        tags: Final = self._base_tags + (
            f"team:{event.team_id}",
            f"route:{event.route}",
            f"rejection:{event.rejection.value}",
        )
        await self._submit([_series(METRIC_FALLBACK_REJECTED, COUNT, 1.0, tags)])

    async def incident(self, event: IncidentEvent) -> None:
        team_id = event.detail.get("team_id")
        tags: tuple[str, ...] = self._base_tags + (f"reason:{event.reason}",)
        if team_id is not None:
            tags += (f"team:{team_id}",)
        await self._submit([_series(METRIC_INCIDENT, COUNT, 1.0, tags)])

    async def _submit(self, series: list[dict[str, Any]]) -> None:
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
