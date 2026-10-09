from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .events import Event
from .policy import Permission
from .rules import GatewaySettings, ModelGroup


@dataclass(frozen=True)
class Caller:
    team_id: str | None
    key_models: tuple[str, ...]
    team_models: tuple[str, ...]
    key_budgets: tuple[str, ...]
    team_max_budget: float | None
    team_spend: float
    own_hold_usd: float


class SpendCounter(Protocol):
    async def read(
        self, team_id: str, fallback_spend: float, max_budget: float
    ) -> float: ...


class Permissions(Protocol):
    def check(self, caller: Caller, model: str) -> Permission: ...


class ResetSchedule(Protocol):
    def window_end(self, budget_duration: str) -> datetime: ...


class Guardrails(Protocol):
    def remerge(self, data: dict) -> dict: ...


class GatewayState(Protocol):
    def model_group(self, name: str) -> ModelGroup | None: ...

    def serves(self, name: str, team_id: str) -> bool: ...

    def settings(self) -> GatewaySettings: ...


class ClaimStore(Protocol):
    async def claim(self, key: str, ttl_seconds: int) -> bool: ...


class EventSink(Protocol):
    async def emit(self, event: Event) -> None: ...
