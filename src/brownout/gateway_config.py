from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any, Final

from .inputs import DocumentError
from .rules import GatewaySettings

GENERAL_SETTINGS: Final = "general_settings"
LITELLM_SETTINGS: Final = "litellm_settings"
ROUTER_SETTINGS: Final = "router_settings"
ROUTER_REDIS_FIELDS: Final = (
    ("redis_host", "host"),
    ("redis_port", "port"),
    ("redis_password", "password"),
    ("redis_url", "url"),
)
ROUTER_REDIS_TARGETS: Final = ("redis_host", "redis_url")
REDIS_CACHE_TYPE: Final = "redis"
PRICE_KEYS: Final = ("input_cost_per_token", "output_cost_per_token")
REDIS_TARGETS: Final = ("host", "url", "startup_nodes", "sentinel_nodes")
REDIS_CONNECTION_FIELDS: Final = (
    "host",
    "port",
    "url",
    "username",
    "password",
    "ssl",
    "db",
    "startup_nodes",
    "sentinel_nodes",
    "service_name",
    "sentinel_password",
)


@dataclass(frozen=True)
class GatewayConfig:
    source: Path
    model_list: tuple[Mapping[str, Any], ...]
    settings: GatewaySettings
    coordination_redis: bool
    cache_redis: Mapping[str, Any] | None
    router_redis: Mapping[str, Any] | None
    callbacks: tuple[str, ...]
    budget_exceeded_status_code: bool


def read_gateway_config(source: Path, config: Mapping[str, Any]) -> GatewayConfig:
    general: Final = _section(source, config, GENERAL_SETTINGS)
    litellm_settings: Final = _section(source, config, LITELLM_SETTINGS)
    return GatewayConfig(
        source=source,
        model_list=_model_list(source, config.get("model_list")),
        settings=settings_from_general(general),
        coordination_redis=bool(
            _optional_mapping(
                source, "coordination_redis", general.get("coordination_redis")
            )
        ),
        cache_redis=_cache_redis(
            _optional_mapping(
                source, "cache_params", litellm_settings.get("cache_params")
            )
        ),
        router_redis=_router_redis(_section(source, config, ROUTER_SETTINGS)),
        callbacks=_callbacks(source, litellm_settings.get("callbacks")),
        budget_exceeded_status_code="budget_exceeded_status_code" in litellm_settings,
    )


def settings_from_general(general: Mapping[str, Any]) -> GatewaySettings:
    return GatewaySettings(
        fail_closed_budget_enforcement=general.get("fail_closed_budget_enforcement")
        is True,
        disable_budget_reservation=general.get("disable_budget_reservation") is True,
    )


def price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if not isfinite(number):
        return None
    return number


def declared_data_class(model_info: Mapping[str, Any]) -> str | None:
    declared: Final = model_info.get("data_class")
    if isinstance(declared, str):
        return declared
    return None


def _section(source: Path, config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return _optional_mapping(source, name, config.get(name))


def _optional_mapping(source: Path, name: str, value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DocumentError(f"{source} {name} must be a mapping")
    return value


def _model_list(source: Path, model_list: Any) -> tuple[Mapping[str, Any], ...]:
    if model_list is None:
        return ()
    if not isinstance(model_list, list):
        raise DocumentError(f"{source} model_list must be a list of deployments")
    return tuple(
        _deployment(source, f"model_list entry {index}", entry)
        for index, entry in enumerate(model_list, start=1)
    )


def _deployment(source: Path, where: str, entry: Any) -> Mapping[str, Any]:
    if not isinstance(entry, Mapping):
        raise DocumentError(f"{source} {where} must be a mapping")
    model_name: Final = entry.get("model_name")
    if not isinstance(model_name, str) or not model_name:
        raise DocumentError(f"{source} {where} needs a model_name string")
    params: Final = _optional_mapping(
        source, f"{where} litellm_params", entry.get("litellm_params")
    )
    model_info: Final = _optional_mapping(
        source, f"{where} model_info", entry.get("model_info")
    )
    model: Final = params.get("model")
    if not isinstance(model, str) or not model:
        raise DocumentError(f"{source} {where} needs a litellm_params.model string")
    for block, fields in (("litellm_params", params), ("model_info", model_info)):
        for key in PRICE_KEYS:
            _require_readable_price(source, where, block, fields, key)
    return dict(entry)


def _require_readable_price(
    source: Path, where: str, block: str, fields: Mapping[str, Any], key: str
) -> None:
    value: Final = fields.get(key)
    if value is not None and price(value) is None:
        raise DocumentError(f"{source} {where} {block}.{key} must be a finite number")


def _callbacks(source: Path, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(
        isinstance(entry, str) for entry in value
    ):
        raise DocumentError(
            f"{source} litellm_settings.callbacks must be a list of callback names"
        )
    return tuple(value)


def _router_redis(router_settings: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if not any(router_settings.get(target) for target in ROUTER_REDIS_TARGETS):
        return None
    return {
        field: router_settings[key]
        for key, field in ROUTER_REDIS_FIELDS
        if key in router_settings
    }


def _cache_redis(cache_params: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if cache_params.get("type") != REDIS_CACHE_TYPE:
        return None
    if not any(cache_params.get(target) for target in REDIS_TARGETS):
        return None
    return {
        field: cache_params[field]
        for field in REDIS_CONNECTION_FIELDS
        if field in cache_params
    }
