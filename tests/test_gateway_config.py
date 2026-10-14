from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest

from brownout.gateway_config import (
    Deployment,
    declared_data_class,
    price,
    read_gateway_config,
    settings_from_general,
)
from brownout.inputs import DocumentError

from .support import lab_gateway_config

SOURCE: Final = Path("config.yaml")
DEFAULT_ALERTS: Final = ("llm_exceptions", "budget_alerts", "outage_alerts")


def read(config: dict[str, Any]) -> Any:
    return read_gateway_config(SOURCE, config, DEFAULT_ALERTS)


def deployment(**fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "model_name": "chat",
        "litellm_params": {"model": "openai/gpt-4o-mini"},
    }
    entry.update(fields)
    return entry


def test_a_config_without_sections_reads_as_litellm_defaults():
    config = read({})

    assert config.deployments == ()
    assert not config.settings.fail_closed_budget_enforcement
    assert not config.settings.disable_budget_reservation
    assert not config.native_budget_alerts
    assert not config.coordination_redis
    assert config.cache_redis is None
    assert config.callbacks == ()
    assert not config.budget_exceeded_status_code


def test_null_sections_read_as_empty():
    config = read(
        {"general_settings": None, "litellm_settings": None, "model_list": None}
    )

    assert (config.deployments, config.callbacks) == ((), ())


def test_the_lab_config_reads_its_deployments():
    config = read_gateway_config(SOURCE, lab_gateway_config(), DEFAULT_ALERTS)

    assert "budget-lab-primary" in {entry.model_name for entry in config.deployments}


@pytest.mark.parametrize(
    ("config", "problem"),
    [
        pytest.param(
            {"general_settings": ["a"]},
            "general_settings must be a mapping",
            id="general-settings",
        ),
        pytest.param(
            {"litellm_settings": "x"},
            "litellm_settings must be a mapping",
            id="litellm-settings",
        ),
        pytest.param(
            {"model_list": {"a": "b"}}, "model_list must be a list", id="model-list"
        ),
        pytest.param(
            {"model_list": ["a"]},
            "model_list entry 1 must be a mapping",
            id="deployment",
        ),
        pytest.param(
            {"model_list": [{"litellm_params": {"model": "x"}}]},
            "model_name",
            id="model-name-missing",
        ),
        pytest.param(
            {"model_list": [deployment(model_name=123)]},
            "model_name",
            id="model-name-a-number",
        ),
        pytest.param(
            {"model_list": [deployment(litellm_params=["x"])]},
            "litellm_params must be a mapping",
            id="litellm-params",
        ),
        pytest.param(
            {"model_list": [deployment(litellm_params={})]},
            "litellm_params.model",
            id="model-missing",
        ),
        pytest.param(
            {"model_list": [deployment(model_info=["x"])]},
            "model_info must be a mapping",
            id="model-info",
        ),
        pytest.param(
            {
                "model_list": [
                    deployment(
                        litellm_params={"model": "x", "input_cost_per_token": "cheap"}
                    )
                ]
            },
            "input_cost_per_token must be a finite number",
            id="price-words",
        ),
        pytest.param(
            {
                "model_list": [
                    deployment(
                        litellm_params={"model": "x", "output_cost_per_token": True}
                    )
                ]
            },
            "output_cost_per_token must be a finite number",
            id="price-boolean",
        ),
        pytest.param(
            {
                "model_list": [
                    deployment(
                        litellm_params={
                            "model": "x",
                            "input_cost_per_token": float("inf"),
                        }
                    )
                ]
            },
            "input_cost_per_token must be a finite number",
            id="price-infinite",
        ),
        pytest.param(
            {
                "model_list": [
                    deployment(
                        litellm_params={
                            "model": "x",
                            "input_cost_per_token": "os.environ/INPUT_PRICE",
                        }
                    )
                ]
            },
            "input_cost_per_token must be a finite number",
            id="price-from-environment",
        ),
        pytest.param(
            {"model_list": [deployment(model_info={"input_cost_per_token": "cheap"})]},
            "model_info.input_cost_per_token must be a finite number",
            id="info-price-words",
        ),
        pytest.param(
            {"model_list": [deployment(model_info={"output_cost_per_token": False})]},
            "model_info.output_cost_per_token must be a finite number",
            id="info-price-boolean",
        ),
        pytest.param(
            {
                "model_list": [
                    deployment(model_info={"input_cost_per_token": float("nan")})
                ]
            },
            "model_info.input_cost_per_token must be a finite number",
            id="info-price-not-a-number",
        ),
        pytest.param(
            {
                "model_list": [
                    deployment(
                        model_info={"output_cost_per_token": "os.environ/OUTPUT_PRICE"}
                    )
                ]
            },
            "model_info.output_cost_per_token must be a finite number",
            id="info-price-from-environment",
        ),
        pytest.param(
            {
                "model_list": [
                    deployment(
                        litellm_params={"model": "x", "input_cost_per_token": 1e-6},
                        model_info={"input_cost_per_token": "cheap"},
                    )
                ]
            },
            "model_info.input_cost_per_token must be a finite number",
            id="info-price-unreadable-under-a-readable-one",
        ),
        pytest.param(
            {"general_settings": {"alert_types": "budget_alerts"}},
            "alert_types must be a list",
            id="alert-types",
        ),
        pytest.param(
            {"general_settings": {"coordination_redis": "redis"}},
            "coordination_redis must be a mapping",
            id="coordination-redis",
        ),
        pytest.param(
            {"litellm_settings": {"callbacks": 3}},
            "callbacks must be a list",
            id="callbacks",
        ),
        pytest.param(
            {"litellm_settings": {"callbacks": [{"a": 1}]}},
            "callbacks must be a list",
            id="callback-entry",
        ),
        pytest.param(
            {"litellm_settings": {"cache_params": ["redis"]}},
            "cache_params must be a mapping",
            id="cache-params",
        ),
    ],
)
def test_a_malformed_config_is_refused_by_field(config, problem):
    with pytest.raises(DocumentError, match=problem):
        read(config)


@pytest.mark.parametrize(
    ("value", "on"),
    [
        (True, True),
        (False, False),
        (None, False),
        ("true", False),
        (1, False),
        ("yes", False),
    ],
)
def test_a_setting_is_on_only_when_it_is_literally_true(value, on):
    settings = settings_from_general(
        {"fail_closed_budget_enforcement": value, "disable_budget_reservation": value}
    )

    assert settings.fail_closed_budget_enforcement is on
    assert settings.disable_budget_reservation is on


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0001, 0.0001),
        (2, 2.0),
        ("1e-4", 0.0001),
        (" 0.5 ", 0.5),
        ("cheap", None),
        ("nan", None),
        (float("inf"), None),
        (True, None),
        (None, None),
        ([0.1], None),
    ],
)
def test_a_price_is_read_the_way_litellm_reads_it(value, expected):
    assert price(value) == expected


@pytest.mark.parametrize(
    ("info", "expected"),
    [({"data_class": "internal"}, "internal"), ({"data_class": 3}, None), ({}, None)],
)
def test_a_data_class_is_a_string_or_undeclared(info, expected):
    assert declared_data_class(info) == expected


def test_a_deployment_carries_its_explicit_prices_and_class():
    config = read(
        {
            "model_list": [
                deployment(
                    litellm_params={
                        "model": "openai/x",
                        "input_cost_per_token": "1e-4",
                        "output_cost_per_token": 0.001,
                    },
                    model_info={"data_class": "internal"},
                )
            ]
        }
    )

    assert config.deployments == (
        Deployment("chat", "openai/x", 0.0001, 0.001, "internal"),
    )


@pytest.mark.parametrize(
    ("params", "info", "prices"),
    [
        pytest.param(
            {},
            {"input_cost_per_token": "3e-6", "output_cost_per_token": 4e-6},
            (3e-6, 4e-6),
            id="model-info-only",
        ),
        pytest.param(
            {"input_cost_per_token": 1e-6},
            {"output_cost_per_token": 8e-6},
            (1e-6, 8e-6),
            id="split-across-both",
        ),
        pytest.param(
            {"input_cost_per_token": 1e-6},
            {"input_cost_per_token": 9e-6, "output_cost_per_token": 8e-6},
            (1e-6, 8e-6),
            id="litellm-params-first",
        ),
        pytest.param({}, {}, (None, None), id="neither"),
    ],
)
def test_a_deployment_takes_each_price_from_litellm_params_then_model_info(
    params, info, prices
):
    config = read(
        {
            "model_list": [
                deployment(
                    litellm_params={"model": "openai/x", **params}, model_info=info
                )
            ]
        }
    )

    priced = config.deployments[0]
    assert (priced.input_cost_per_token, priced.output_cost_per_token) == prices


@pytest.mark.parametrize(
    ("general", "native"),
    [
        ({}, False),
        ({"alerting": ["slack"]}, True),
        ({"alerting": ["slack"], "alert_types": ["budget_alerts"]}, True),
        ({"alerting": ["slack"], "alert_types": ["outage_alerts"]}, False),
        ({"alert_types": ["budget_alerts"]}, False),
    ],
)
def test_native_budget_alerts_follow_alerting_and_its_types(general, native):
    assert read({"general_settings": general}).native_budget_alerts is native


def test_alert_types_default_to_litellms_when_alerting_lists_none():
    config = read({"general_settings": {"alerting": ["slack"]}})

    assert config.alert_types == DEFAULT_ALERTS


@pytest.mark.parametrize(
    ("callbacks", "expected"),
    [
        ("langfuse", ("langfuse",)),
        (["langfuse", "prometheus"], ("langfuse", "prometheus")),
    ],
)
def test_callbacks_read_as_a_list(callbacks, expected):
    assert read({"litellm_settings": {"callbacks": callbacks}}).callbacks == expected


@pytest.mark.parametrize(
    ("cache_params", "expected"),
    [
        (
            {"type": "redis", "host": "redis", "port": 6379, "ttl": 600},
            {"host": "redis", "port": 6379},
        ),
        ({"type": "redis"}, None),
        ({"type": "local", "host": "redis"}, None),
        (None, None),
    ],
)
def test_a_redis_cache_offers_its_connection(cache_params, expected):
    config = read({"litellm_settings": {"cache_params": cache_params}})

    assert config.cache_redis == expected


@pytest.mark.parametrize(
    ("value", "configured"), [({"host": "redis"}, True), ({}, False), (None, False)]
)
def test_coordination_redis_counts_when_it_names_a_connection(value, configured):
    assert (
        read({"general_settings": {"coordination_redis": value}}).coordination_redis
        is configured
    )


def test_a_status_code_counts_when_it_is_set():
    config = read({"litellm_settings": {"budget_exceeded_status_code": 422}})

    assert config.budget_exceeded_status_code
