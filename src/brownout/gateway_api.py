from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

from .rules import ModelGroup, group_price, has_usable_prices

KEY_PAGE_SIZE: Final = 100

PageProgress = Callable[[int, int], None]


class GatewayReadError(Exception):
    pass


@dataclass(frozen=True)
class LiteLLMTeam:
    team_id: str
    max_budget: float | None
    budget_duration: str | None
    models: tuple[str, ...]
    budgeted_members: tuple[str, ...] = ()


@dataclass(frozen=True)
class LiteLLMKey:
    token: str
    alias: str | None
    team_id: str | None
    own_budgets: tuple[str, ...]


@dataclass(frozen=True)
class LiteLLMModel:
    name: str
    priced: bool


@dataclass(frozen=True)
class GatewaySnapshot:
    teams: tuple[LiteLLMTeam, ...]
    keys: tuple[LiteLLMKey, ...]
    models: tuple[LiteLLMModel, ...]
    groups: Mapping[str, ModelGroup]


def read_gateway(client: httpx.Client, on_page: PageProgress) -> GatewaySnapshot:
    deployments: Final = _deployments(client)
    return GatewaySnapshot(
        teams=_teams(client),
        keys=_keys(client, on_page),
        models=_models(deployments),
        groups=_groups(deployments),
    )


def read_model_groups(client: httpx.Client) -> dict[str, ModelGroup]:
    return _groups(_deployments(client))


def _teams(client: httpx.Client) -> tuple[LiteLLMTeam, ...]:
    listed: Final = _get(client, "/team/list", {})
    if not isinstance(listed, list):
        raise GatewayReadError("/team/list did not return a list of teams")
    return tuple(_team(entry) for entry in listed)


def _team(entry: Any) -> LiteLLMTeam:
    if not isinstance(entry, dict) or not isinstance(entry.get("team_id"), str):
        raise GatewayReadError("/team/list returned a team without a team_id")
    max_budget = entry.get("max_budget")
    if max_budget is not None:
        max_budget = float(max_budget)
    return LiteLLMTeam(
        team_id=entry["team_id"],
        max_budget=max_budget,
        budget_duration=entry.get("budget_duration"),
        models=tuple(str(model) for model in entry.get("models") or []),
        budgeted_members=_budgeted_members(entry.get("team_memberships") or []),
    )


def _budgeted_members(memberships: Any) -> tuple[str, ...]:
    if not isinstance(memberships, list):
        raise GatewayReadError(
            "/team/list returned team_memberships that are not a list"
        )
    return tuple(
        str(membership["user_id"])
        for membership in memberships
        if isinstance(membership, dict) and _member_budget(membership) is not None
    )


def _member_budget(membership: Mapping[str, Any]) -> Any:
    table: Final = membership.get("litellm_budget_table")
    if not isinstance(table, dict):
        return None
    return table.get("max_budget")


def _keys(client: httpx.Client, on_page: PageProgress) -> tuple[LiteLLMKey, ...]:
    keys: list[LiteLLMKey] = []
    page = 1
    while True:
        listed = _get(
            client,
            "/key/list",
            {"return_full_object": "true", "page": page, "size": KEY_PAGE_SIZE},
        )
        if not isinstance(listed, dict) or not isinstance(listed.get("keys"), list):
            raise GatewayReadError("/key/list did not return a page of keys")
        total_pages = int(listed.get("total_pages") or 1)
        on_page(page, total_pages)
        keys.extend(_key(entry) for entry in listed["keys"])
        if page >= total_pages:
            return tuple(keys)
        page += 1


def _key(entry: Any) -> LiteLLMKey:
    if not isinstance(entry, dict) or not isinstance(entry.get("token"), str):
        raise GatewayReadError("/key/list returned a key without a token")
    declared = {
        "max_budget": entry.get("max_budget") is not None,
        "model_max_budget": bool(entry.get("model_max_budget")),
        "budget_limits": bool(entry.get("budget_limits")),
    }
    return LiteLLMKey(
        token=entry["token"],
        alias=entry.get("key_alias"),
        team_id=entry.get("team_id"),
        own_budgets=tuple(name for name, present in declared.items() if present),
    )


def _deployments(client: httpx.Client) -> list[Mapping[str, Any]]:
    listed: Final = _get(client, "/model/info", {})
    if not isinstance(listed, dict) or not isinstance(listed.get("data"), list):
        raise GatewayReadError("/model/info did not return a list of deployments")
    return [deployment for deployment in listed["data"] if isinstance(deployment, dict)]


def _models(deployments: list[Mapping[str, Any]]) -> tuple[LiteLLMModel, ...]:
    priced: dict[str, bool] = {}
    for deployment in deployments:
        name = str(deployment.get("model_name") or "")
        priced[name] = priced.get(name, False) or _has_prices(
            deployment.get("model_info") or {}
        )
    return tuple(LiteLLMModel(name, priced[name]) for name in sorted(priced))


def _groups(deployments: list[Mapping[str, Any]]) -> dict[str, ModelGroup]:
    infos: dict[str, list[Mapping[str, Any]]] = {}
    for deployment in deployments:
        name = str(deployment.get("model_name") or "")
        infos.setdefault(name, []).append(deployment.get("model_info") or {})
    return {name: _group(name, group_infos) for name, group_infos in infos.items()}


def _group(name: str, infos: list[Mapping[str, Any]]) -> ModelGroup:
    return ModelGroup(
        name=name,
        input_cost_per_token=group_price(
            [_number(info.get("input_cost_per_token")) for info in infos]
        ),
        output_cost_per_token=group_price(
            [_number(info.get("output_cost_per_token")) for info in infos]
        ),
        data_classes=tuple(_data_class(info) for info in infos),
    )


def _data_class(info: Mapping[str, Any]) -> str | None:
    declared: Final = info.get("data_class")
    if isinstance(declared, str):
        return declared
    return None


def _has_prices(model_info: Mapping[str, Any]) -> bool:
    return has_usable_prices(
        _number(model_info.get("input_cost_per_token")),
        _number(model_info.get("output_cost_per_token")),
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _get(client: httpx.Client, path: str, params: Mapping[str, Any]) -> Any:
    try:
        response = client.get(path, params=dict(params))
    except httpx.HTTPError as error:
        raise GatewayReadError(f"{path} could not be reached: {error}") from error
    if response.status_code != httpx.codes.OK:
        raise GatewayReadError(f"{path} answered {response.status_code}")
    try:
        return response.json()
    except ValueError as error:
        raise GatewayReadError(f"{path} did not return JSON") from error
