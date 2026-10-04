from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Final

import redis
from litellm.proxy._types import UserAPIKeyAuth

from budget_policy.adapter import BudgetPolicyHandler, TeamSpendReader
from budget_policy.catalog import PriceCatalog
from budget_policy.manifest import Manifest, TeamPolicy
from budget_policy.signals import (
    FallbackRejectedEvent,
    IncidentEvent,
    RouteDegradedEvent,
    TierEvent,
)

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
CONFIG_DIR: Final = REPO_ROOT / "config"


def lab_redis_url() -> str:
    host: Final = os.getenv("LAB_REDIS_HOST", "localhost")
    port: Final = os.getenv("LAB_REDIS_PORT", "6379")
    password: Final = os.getenv("REDIS_PASSWORD", "")
    if password:
        credentials = f":{password}@"
    else:
        credentials = ""
    return f"redis://{credentials}{host}:{port}/0"


def lab_spend_counter(team_id: str) -> float:
    client = redis.from_url(lab_redis_url())
    try:
        total = 0.0
        for key in client.scan_iter(match=f"*spend:team:{team_id}*"):
            value = client.get(key)
            if value is not None:
                total += float(value)
        return total
    finally:
        client.close()


class FakeSpendReader(TeamSpendReader):
    def __init__(self, spend: float | Exception) -> None:
        self._spend: Final = spend
        self.calls: list[dict[str, Any]] = []

    async def read(
        self, team_id: str, fallback_spend: float, max_budget: float
    ) -> float:
        self.calls.append(
            {
                "team_id": team_id,
                "fallback_spend": fallback_spend,
                "max_budget": max_budget,
            }
        )
        if isinstance(self._spend, Exception):
            raise self._spend
        return self._spend


class FakeStore:
    def __init__(self) -> None:
        self.claimed: set[str] = set()

    async def claim(self, key: str, ttl_seconds: int) -> bool:
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


class RecordingEmitter:
    def __init__(self) -> None:
        self.tiers: list[TierEvent] = []
        self.incidents: list[IncidentEvent] = []
        self.fallback_rejections: list[FallbackRejectedEvent] = []
        self.route_degradations: list[RouteDegradedEvent] = []

    async def emit_tier(self, event: TierEvent) -> None:
        self.tiers.append(event)

    async def emit_fallback_rejected(self, event: FallbackRejectedEvent) -> None:
        self.fallback_rejections.append(event)

    async def emit_route_degraded(self, event: RouteDegradedEvent) -> None:
        self.route_degradations.append(event)

    async def emit_incident(self, event: IncidentEvent) -> None:
        self.incidents.append(event)


class SingleTeamManifest(Manifest):
    def __init__(self, team: TeamPolicy) -> None:
        super().__init__(version="test", teams={team.team_id: team})


def build_handler(
    team: TeamPolicy,
    emitter: RecordingEmitter,
    spend: float | Exception,
    catalog: Any | None = None,
    store: FakeStore | None = None,
) -> BudgetPolicyHandler:
    return BudgetPolicyHandler(
        catalog=catalog
        if catalog is not None
        else PriceCatalog.from_file(CONFIG_DIR / "prices.yaml"),
        manifest=SingleTeamManifest(team),
        store=store if store is not None else FakeStore(),
        emitter=emitter,
        spend_reader=FakeSpendReader(spend),
    )


def make_auth(
    team: TeamPolicy,
    reserved_cost: float = 0.0,
    applied_adjustment: float = 0.0,
) -> Any:
    auth = UserAPIKeyAuth(
        api_key="sk-lab",
        team_id=team.team_id,
        team_alias=team.team_id,
        team_max_budget=team.envelope_usd,
        team_spend=0.0,
    )
    if reserved_cost or applied_adjustment:
        auth.budget_reservation = {
            "reserved_cost": reserved_cost,
            "entries": [
                {
                    "counter_key": f"spend:team:{team.team_id}",
                    "reserved_cost": reserved_cost,
                    "applied_adjustment": applied_adjustment,
                },
                {
                    "counter_key": "spend:key:sk-lab",
                    "reserved_cost": reserved_cost,
                    "applied_adjustment": applied_adjustment,
                },
            ],
            "finalized": False,
        }
    return auth
