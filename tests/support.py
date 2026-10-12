from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, assert_never

import redis
import yaml
from litellm.proxy._types import UserAPIKeyAuth

from brownout import bridge
from brownout.cli import gateway_file
from brownout.events import (
    Event,
    FallbackRejectedEvent,
    IncidentEvent,
    RouteDegradedEvent,
    TierEvent,
)
from brownout.gateway import BrownoutHandler
from brownout.manifest import Manifest, TeamPolicy
from brownout.policy import Permission
from brownout.ports import Caller, Guardrails, Permissions, ResetSchedule
from brownout.rules import GatewaySettings, ModelGroup

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
CONFIG_DIR: Final = REPO_ROOT / "config"
DATA_CLASS_ORDER: Final = ("internal", "customer_pii")
FIELD_KEYS: Final = frozenset(
    {
        "requested_route",
        "outcome",
        "served_route",
        "budget_tier",
        "route_tier",
        "degradation_reason",
        "utilisation_percent",
        "refusal",
    }
)


def lab_redis_url() -> str:
    host: Final = os.getenv("LAB_REDIS_HOST", "localhost")
    port: Final = os.getenv("LAB_REDIS_PORT", "16379")
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


def lab_gateway_config() -> Mapping[str, Any]:
    with open(CONFIG_DIR / "config.yaml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class FakeSpendCounter:
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


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.tiers: list[TierEvent] = []
        self.route_degradations: list[RouteDegradedEvent] = []
        self.fallback_rejections: list[FallbackRejectedEvent] = []
        self.incidents: list[IncidentEvent] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)
        match event:
            case TierEvent():
                self.tiers.append(event)
            case RouteDegradedEvent():
                self.route_degradations.append(event)
            case FallbackRejectedEvent():
                self.fallback_rejections.append(event)
            case IncidentEvent():
                self.incidents.append(event)
            case _:
                assert_never(event)


class FakeGateway:
    def __init__(
        self,
        groups: Mapping[str, ModelGroup],
        settings: GatewaySettings,
        extra_served: frozenset[str] = frozenset(),
    ) -> None:
        self.groups: Final = dict(groups)
        self._settings = settings
        self._extra_served: Final = extra_served

    @classmethod
    def from_lab_config(cls) -> FakeGateway:
        lab: Final = gateway_file(lab_gateway_config())
        return cls(lab.groups, lab.settings)

    def model_group(self, name: str) -> ModelGroup | None:
        return self.groups.get(name)

    def serves(self, name: str, team_id: str) -> bool:
        return name in self.groups or name in self._extra_served

    def settings(self) -> GatewaySettings:
        return self._settings

    def with_settings(self, settings: GatewaySettings) -> FakeGateway:
        return FakeGateway(self.groups, settings, self._extra_served)

    @property
    def current_settings(self) -> GatewaySettings:
        return self._settings

    def with_group(self, group: ModelGroup) -> FakeGateway:
        return FakeGateway(
            {**self.groups, group.name: group}, self._settings, self._extra_served
        )

    def without_group(self, name: str) -> FakeGateway:
        groups = {key: value for key, value in self.groups.items() if key != name}
        return FakeGateway(groups, self._settings, self._extra_served)

    def serving(self, *names: str) -> FakeGateway:
        return FakeGateway(self.groups, self._settings, frozenset(names))


class SequenceSchedule:
    def __init__(self, window_ends: list[datetime]) -> None:
        self._window_ends: Final[Iterator[datetime]] = iter(window_ends)

    def window_end(self, budget_duration: str) -> datetime:
        return next(self._window_ends)


class FixedSchedule:
    def window_end(self, budget_duration: str) -> datetime:
        return datetime(2026, 10, 12, tzinfo=UTC)


class FixedPermissions:
    def __init__(self, answer: Permission) -> None:
        self._answer: Final = answer

    def check(self, caller: Caller, model: str) -> Permission:
        return self._answer


class RecordingPermissions:
    def __init__(self, inner: Permissions) -> None:
        self._inner: Final = inner
        self.checked: list[str] = []

    def check(self, caller: Caller, model: str) -> Permission:
        self.checked.append(model)
        return self._inner.check(caller, model)


class FailingGuardrails:
    def remerge(self, data: dict) -> dict:
        raise RuntimeError("guardrail merge failed")


def manifest_for(team: TeamPolicy) -> Manifest:
    return Manifest(
        version="test", data_class_order=DATA_CLASS_ORDER, teams={team.team_id: team}
    )


def build_handler(
    team: TeamPolicy,
    sink: RecordingSink,
    spend: float | Exception,
    store: FakeStore | None = None,
    gateway: FakeGateway | None = None,
    schedule: ResetSchedule | None = None,
    permissions: Permissions | None = None,
    guardrails: Guardrails | None = None,
) -> BrownoutHandler:
    if store is None:
        store = FakeStore()
    if gateway is None:
        gateway = FakeGateway.from_lab_config()
    if schedule is None:
        schedule = FixedSchedule()
    if permissions is None:
        permissions = bridge.LiteLLMPermissions(lambda: None)
    if guardrails is None:
        guardrails = bridge.LiteLLMGuardrails(lambda: None)
    return BrownoutHandler(
        manifest=manifest_for(team),
        spend_counter=FakeSpendCounter(spend),
        permissions=permissions,
        schedule=schedule,
        guardrails=guardrails,
        gateway=gateway,
        claims=store,
        events=sink,
    )


def make_auth(
    team: TeamPolicy, reserved_cost: float = 0.0, applied_adjustment: float = 0.0
) -> UserAPIKeyAuth:
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


async def request(
    handler: BrownoutHandler,
    auth: UserAPIKeyAuth,
    model: str,
    extra: Mapping[str, Any] | None = None,
) -> dict:
    data: dict[str, Any] = {"model": model, "messages": []}
    if extra is not None:
        data.update(extra)
    return await handler.async_pre_call_hook(
        user_api_key_dict=auth, cache=None, data=data, call_type="acompletion"
    )


def disclosed(data: dict) -> dict[str, Any]:
    return data["metadata"]["spend_logs_metadata"]["brownout"]


async def headers_for(handler: BrownoutHandler, applied: dict) -> dict[str, str]:
    result = await handler.async_post_call_response_headers_hook(
        data=applied, user_api_key_dict=None, response=object()
    )
    assert result is not None
    return result
