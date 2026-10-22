from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import logging
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, get_args

import litellm
import pytest
from litellm._logging import JsonFormatter
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils import litellm_logging
from litellm.litellm_core_utils.core_helpers import (
    get_metadata_variable_name_from_kwargs,
)
from litellm.proxy import proxy_server
from litellm.proxy._types import ProxyException, UserAPIKeyAuth
from litellm.proxy.auth import auth_checks
from litellm.proxy.common_request_processing import ProxyRouteType
from litellm.proxy.common_utils.timezone_utils import get_budget_reset_time
from litellm.proxy.spend_tracking import budget_reservation
from litellm.proxy.utils import _check_and_merge_model_level_guardrails
from litellm.types.utils import CallTypes

from brownout import bridge, log_output

LITELLM_ROOT: Final = Path(importlib.util.find_spec("litellm").origin).parent
SERVER_DEFAULT_INLINE: Final = 'general_settings.get("completion_model", None)'
SERVER_DEFAULT_RESOLVER: Final = 'self.data["model"] = resolve_inference_model('
GUARDRAIL_MERGE_NAMES: Final = (
    "_check_and_merge_model_level_guardrails",
    "check_and_merge_model_level_guardrails",
)
TEAM_BUDGET_CHECK_NAMES: Final = frozenset(
    {"_team_max_budget_check", "team_max_budget_check"}
)
OVERRIDE_MERGE_FORMS: Final = (
    "if key in override_settings and key not in data:",
    "if override_settings.get(key) is not None and key not in data:",
)


def _function_in_source(
    relative_path: str, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return _function_under_any_name(relative_path, frozenset({name}))


def _function_under_any_name(
    relative_path: str, names: frozenset[str]
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse((LITELLM_ROOT / relative_path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name in names
        ):
            return node
    raise AssertionError(f"{relative_path} no longer defines {sorted(names)}")


def _parameter_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    arguments = function.args
    return {argument.arg for argument in arguments.args + arguments.kwonlyargs}


def test_seam_spend_logs_metadata_reaches_every_logging_integration():
    assert "spend_logs_metadata" in litellm_logging._STANDARD_LOGGING_METADATA_KEYS


def test_seam_the_pre_call_and_header_hooks_keep_their_parameters():
    pre_call = inspect.signature(CustomLogger.async_pre_call_hook).parameters
    headers = inspect.signature(
        CustomLogger.async_post_call_response_headers_hook
    ).parameters

    assert {"user_api_key_dict", "cache", "data", "call_type"} <= set(pre_call)
    assert {"data", "user_api_key_dict", "response", "litellm_call_info"} <= set(
        headers
    )


def test_seam_the_permission_check_keeps_its_parameters():
    parameters = inspect.signature(auth_checks._can_object_call_model).parameters

    assert {"model", "llm_router", "models", "team_id", "object_type"} <= set(
        parameters
    )
    assert list(
        inspect.signature(auth_checks._resolve_key_models_for_auth_check).parameters
    ) == ["valid_token"]


def test_seam_the_guardrail_merge_keeps_its_parameters():
    parameters = inspect.signature(_check_and_merge_model_level_guardrails).parameters

    assert {"data", "llm_router", "trust_client_model_info"} <= set(parameters)


def test_seam_guardrails_merge_before_the_pre_call_hook():
    source = (LITELLM_ROOT / "proxy/common_request_processing.py").read_text(
        encoding="utf-8"
    )
    hook = source.index("self.data = await proxy_logging_obj.pre_call_hook(")

    assert any(
        f"self.data = {name}(" in source[:hook] for name in GUARDRAIL_MERGE_NAMES
    )


def test_seam_a_reservation_entry_carries_the_fields_the_hold_is_read_from():
    counter = budget_reservation._BudgetCounter(
        counter_key="spend:team:t",
        max_budget=1.0,
        fallback_spend=0.0,
        entity_type="Team",
        entity_id="t",
    )

    entry = budget_reservation._counter_to_reservation_entry(
        counter=counter, reserved_cost=0.5
    )

    assert {"counter_key", "reserved_cost", "applied_adjustment"} <= set(entry)


def test_seam_the_team_counter_key_is_spend_team_id():
    check = _function_under_any_name(
        "proxy/auth/auth_checks.py", TEAM_BUDGET_CHECK_NAMES
    )

    assert 'f"spend:team:{team_object.team_id}"' in ast.unparse(check).replace("'", '"')


def test_seam_the_spend_read_accepts_the_arguments_the_plugin_passes():
    read = _function_in_source("proxy/proxy_server.py", "get_current_spend")

    assert {"counter_key", "fallback_spend", "max_budget"} <= _parameter_names(read)


def test_seam_fail_closed_refuses_a_worst_case_that_does_not_fit():
    policy = _function_in_source(
        "proxy/spend_tracking/budget_reservation.py",
        "_apply_over_budget_reservation_policy",
    )

    assert "fail_closed_budget_enforcement" in _parameter_names(policy)


@pytest.mark.parametrize(
    "key", ["fallbacks", "context_window_fallbacks", "content_policy_fallbacks"]
)
def test_seam_a_request_fallback_list_replaces_the_routers_own(key):
    source = (LITELLM_ROOT / "router.py").read_text(encoding="utf-8")

    assert f'kwargs.get("{key}", self.{key})' in source
    assert f'kwargs.pop("{key}", self.{key})' in source


def test_seam_key_and_team_router_settings_skip_keys_the_request_holds():
    source = (LITELLM_ROOT / "proxy/route_llm_request.py").read_text(encoding="utf-8")

    assert any(form in source for form in OVERRIDE_MERGE_FORMS)


def test_seam_the_authenticated_object_carries_the_fields_the_plugin_reads():
    fields = set(UserAPIKeyAuth.model_fields)

    assert {
        "team_id",
        "team_max_budget",
        "team_spend",
        "team_models",
        "models",
        "max_budget",
        "budget_reservation",
    } <= fields


def test_seam_the_reset_schedule_follows_the_gateway_settings(monkeypatch):
    monkeypatch.setattr(litellm, "timezone", "Asia/Kolkata", raising=False)
    monkeypatch.setattr(litellm, "budget_reset_time", "12:00", raising=False)

    window_end = get_budget_reset_time("7d").astimezone(UTC)

    assert window_end > datetime.now(UTC)
    assert (window_end.hour, window_end.minute) == (6, 30)


@pytest.fixture
def lab_router(monkeypatch) -> litellm.Router:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    return litellm.Router(
        model_list=[
            {
                "model_name": "seam-primary",
                "litellm_params": {
                    "model": "openai/seam-primary",
                    "api_key": "sk-seam",
                    "input_cost_per_token": 0.0001,
                    "output_cost_per_token": 0.001,
                },
                "model_info": {"data_class": "internal"},
            }
        ]
    )


def test_seam_the_router_reports_a_group_price(lab_router):
    info = lab_router.get_model_group_info(model_group="seam-primary")

    assert info is not None
    assert (info.input_cost_per_token, info.output_cost_per_token) == (0.0001, 0.001)
    assert lab_router.get_model_group_info(model_group="never-configured") is None


def test_seam_the_router_keeps_custom_model_info(lab_router):
    deployments = lab_router.get_model_list(model_name="seam-primary")

    assert [
        deployment["model_info"].get("data_class") for deployment in deployments
    ] == ["internal"]


def test_seam_the_router_lists_its_model_groups(lab_router):
    assert "seam-primary" in lab_router.get_model_names()


@pytest.fixture
def naming_router(monkeypatch) -> litellm.Router:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    return litellm.Router(
        model_list=[
            {
                "model_name": "openai/*",
                "litellm_params": {"model": "openai/*", "api_key": "sk-seam"},
            },
            {
                "model_name": "seam-mixed",
                "litellm_params": {
                    "model": "openai/seam-priced",
                    "api_key": "sk-seam",
                    "input_cost_per_token": 1e-6,
                    "output_cost_per_token": 2e-6,
                },
            },
            {
                "model_name": "seam-mixed",
                "litellm_params": {
                    "model": "openai/seam-never-mapped",
                    "api_key": "sk-seam",
                },
            },
            {
                "model_name": "seam-model-info",
                "litellm_params": {
                    "model": "openai/seam-never-mapped",
                    "api_key": "sk-seam",
                },
                "model_info": {
                    "input_cost_per_token": 1e-6,
                    "output_cost_per_token": 2e-6,
                },
            },
        ],
        model_group_alias={
            "seam-alias": "seam-mixed",
            "seam-hidden": {"model": "seam-mixed", "hidden": True},
        },
    )


def test_seam_a_wildcard_deployment_prices_a_name_no_group_has(naming_router):
    info = naming_router.get_model_group_info(model_group="openai/gpt-4o")

    assert info is not None
    assert info.input_cost_per_token is not None
    assert "openai/gpt-4o" not in naming_router.get_model_names()


def test_seam_a_router_alias_reports_the_price_of_its_group(naming_router):
    alias = naming_router.get_model_group_info(model_group="seam-alias")
    group = naming_router.get_model_group_info(model_group="seam-mixed")

    assert alias is not None
    assert group is not None
    assert (alias.input_cost_per_token, alias.output_cost_per_token) == (
        group.input_cost_per_token,
        group.output_cost_per_token,
    )


def test_seam_a_hidden_router_alias_reports_no_group(naming_router):
    assert naming_router.get_model_group_info(model_group="seam-hidden") is None


def test_seam_a_group_reports_the_highest_price_any_deployment_knows(naming_router):
    info = naming_router.get_model_group_info(model_group="seam-mixed")

    assert info is not None
    assert (info.input_cost_per_token, info.output_cost_per_token) == (1e-6, 2e-6)


def test_seam_a_price_in_model_info_prices_the_group(naming_router):
    info = naming_router.get_model_group_info(model_group="seam-model-info")

    assert info is not None
    assert (info.input_cost_per_token, info.output_cost_per_token) == (1e-6, 2e-6)


def test_seam_team_and_key_aliases_apply_before_the_pre_call_hook():
    source = (LITELLM_ROOT / "proxy/common_request_processing.py").read_text(
        encoding="utf-8"
    )
    aliases = ast.unparse(
        _function_in_source(
            "proxy/litellm_pre_call_utils.py", "add_litellm_data_to_request"
        )
    )

    assert source.index("self.data = await add_litellm_data_to_request(") < (
        source.index("self.data = await proxy_logging_obj.pre_call_hook(")
    )
    assert "_update_model_if_team_alias_exists(" in aliases
    assert "_update_model_if_key_alias_exists(" in aliases


def _pre_call_source() -> str:
    return (LITELLM_ROOT / "proxy/common_request_processing.py").read_text(
        encoding="utf-8"
    )


def _before_the_pre_call_hook() -> str:
    source = _pre_call_source()
    return source[: source.index("self.data = await proxy_logging_obj.pre_call_hook(")]


def _resolver_puts_the_server_default_first() -> bool:
    resolver = _function_in_source(
        "proxy/common_utils/http_parsing_utils.py", "resolve_inference_model"
    )
    completion = [
        ast.unparse(statement)
        for node in ast.walk(resolver)
        if isinstance(node, ast.match_case)
        and ast.unparse(node.pattern) == "'completion'"
        for statement in node.body
    ]
    return completion == [
        "return settings.get('completion_model') or cli_model or endpoint_model or body_model"
    ]


def test_seam_the_server_default_model_applies_before_the_pre_call_hook():
    before = _before_the_pre_call_hook()

    assert SERVER_DEFAULT_INLINE in before or (
        SERVER_DEFAULT_RESOLVER in before and _resolver_puts_the_server_default_first()
    )


@pytest.mark.parametrize(
    "rewrite",
    [
        'litellm.model_alias_map[self.data["model"]]',
        'user_api_key_dict.aliases[self.data["model"]]',
    ],
)
def test_seam_server_and_key_aliases_apply_before_the_pre_call_hook(rewrite):
    assert rewrite in _before_the_pre_call_hook()


def test_seam_key_and_team_router_aliases_apply_before_the_pre_call_hook():
    source = _pre_call_source()

    assert source.index("_resolve_per_request_model_group_alias(") < source.index(
        "self.data = await proxy_logging_obj.pre_call_hook("
    )


def test_seam_a_local_rate_limit_retries_the_pre_call_logic_on_a_router_fallback():
    source = _pre_call_source()

    assert "except ProxyRateLimitError as original_exc:" in source
    assert '.get("disable_fallbacks")' in source


def test_seam_a_setting_lets_a_user_budget_refuse_team_keys():
    check = ast.unparse(
        _function_in_source("proxy/auth/auth_checks.py", "common_checks")
    )

    assert "apply_user_budget_to_team_keys" in check


def test_seam_a_user_budget_skips_team_keys_unless_the_setting_is_true():
    check = ast.unparse(
        _function_in_source("proxy/auth/auth_checks.py", "common_checks")
    )

    assert (
        "is_team_key and general_settings.get('apply_user_budget_to_team_keys') is not True"
        in check
    )


def test_seam_a_model_outside_the_allowlist_raises_a_proxy_exception():
    with pytest.raises(ProxyException):
        auth_checks._can_object_call_model(
            model="seam-outside",
            llm_router=None,
            models=["seam-inside"],
            team_id="seam-team",
            object_type="team",
        )


def test_seam_the_deployment_filter_hook_keeps_its_parameters():
    parameters = inspect.signature(CustomLogger.async_filter_deployments).parameters

    assert {"model", "healthy_deployments", "messages", "request_kwargs"} <= set(
        parameters
    )


def test_seam_the_router_filters_deployments_after_resolving_the_name():
    healthy = ast.unparse(
        _function_in_source("router.py", "async_get_healthy_deployments")
    )
    callbacks = ast.unparse(
        _function_in_source("router.py", "async_callback_filter_deployments")
    )

    assert healthy.index("_common_checks_available_deployment(") < healthy.index(
        "async_callback_filter_deployments("
    )
    assert "raise e" in callbacks


def test_seam_the_proxy_writes_the_team_into_request_metadata():
    source = (LITELLM_ROOT / "proxy/litellm_pre_call_utils.py").read_text(
        encoding="utf-8"
    )

    assert "user_api_key_team_id=user_api_key_dict.team_id" in source


@pytest.fixture
def billing_router(monkeypatch) -> litellm.Router:
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    return litellm.Router(
        model_list=[
            {
                "model_name": "seam-explicit",
                "litellm_params": {
                    "model": "openai/seam-explicit",
                    "api_key": "sk-seam",
                    "input_cost_per_token": 1e-6,
                    "output_cost_per_token": 2e-6,
                },
            },
            {
                "model_name": "seam-info",
                "litellm_params": {"model": "openai/seam-info", "api_key": "sk-seam"},
                "model_info": {
                    "input_cost_per_token": 3e-6,
                    "output_cost_per_token": 4e-6,
                },
            },
            {
                "model_name": "seam-split",
                "litellm_params": {
                    "model": "openai/seam-split",
                    "api_key": "sk-seam",
                    "input_cost_per_token": 1e-6,
                },
                "model_info": {
                    "input_cost_per_token": 9e-6,
                    "output_cost_per_token": 8e-6,
                },
            },
            {
                "model_name": "seam-mapped",
                "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "sk-seam"},
            },
            {
                "model_name": "seam-unpriced",
                "litellm_params": {
                    "model": "openai/seam-unpriced",
                    "api_key": "sk-seam",
                },
            },
        ],
        model_group_alias={"seam-alias": "seam-explicit"},
    )


def _billed(router: litellm.Router, name: str) -> tuple[object, object]:
    deployment = router.get_model_list(model_name=name)[0]
    info = router.get_deployment_model_info(
        model_id=deployment["model_info"]["id"],
        model_name=deployment["litellm_params"]["model"],
    )
    assert info is not None
    return (info.get("input_cost_per_token"), info.get("output_cost_per_token"))


@pytest.mark.parametrize(
    ("name", "prices"),
    [
        pytest.param("seam-explicit", (1e-6, 2e-6), id="litellm-params"),
        pytest.param("seam-info", (3e-6, 4e-6), id="model-info"),
        pytest.param("seam-split", (1e-6, 8e-6), id="litellm-params-first"),
        pytest.param("seam-mapped", (1.5e-7, 6e-7), id="cost-map"),
    ],
)
def test_seam_a_deployment_reports_the_price_litellm_bills(
    billing_router, name, prices
):
    assert _billed(billing_router, name) == prices


def test_seam_an_unpriced_deployment_reports_no_price(billing_router):
    assert not any(_billed(billing_router, "seam-unpriced"))


def test_seam_the_router_lists_configured_model_names_only(billing_router):
    names = {deployment["model_name"] for deployment in billing_router.model_list}

    assert "seam-explicit" in names
    assert "seam-alias" not in names


def test_seam_every_admitted_call_type_is_one_litellm_names():
    named = {call_type.value for call_type in CallTypes} | set(get_args(ProxyRouteType))

    assert set(bridge.CALL_KINDS) - named == set()


@pytest.mark.parametrize(
    ("data", "container"),
    [
        ({"metadata": {}}, "metadata"),
        ({}, "metadata"),
        ({"metadata": {}, "litellm_metadata": {}}, "litellm_metadata"),
    ],
)
def test_seam_litellm_keeps_its_metadata_in_litellm_metadata_when_present(
    data, container
):
    assert get_metadata_variable_name_from_kwargs(data) == container


def test_seam_an_mcp_tool_call_runs_the_pre_call_hook_with_its_call_type():
    source = (
        LITELLM_ROOT / "proxy/_experimental/mcp_server/mcp_server_manager.py"
    ).read_text(encoding="utf-8")

    assert "call_type=CallTypes.call_mcp_tool.value" in source


AZURE_DEPLOYMENT: Final = {
    "model": "azure/my-custom-deployment",
    "api_key": "sk-seam",
    "api_base": "https://seam.openai.azure.com",
    "api_version": "2024-06-01",
}


def test_seam_building_a_router_opens_no_connection(monkeypatch):
    attempts: list[object] = []

    def refuse(sock: socket.socket, address: object) -> None:
        attempts.append(address)
        raise OSError("a router build tried to connect")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")

    litellm.Router(
        model_list=[
            {"model_name": "openai", "litellm_params": {"model": "openai/gpt-4o"}},
            {"model_name": "azure", "litellm_params": dict(AZURE_DEPLOYMENT)},
            {
                "model_name": "bedrock",
                "litellm_params": {
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                    "aws_region_name": "us-east-1",
                },
            },
        ]
    )

    assert attempts == []


@pytest.mark.parametrize(
    ("deployment", "priced"),
    [
        pytest.param(
            {
                "model_name": "chat",
                "litellm_params": dict(AZURE_DEPLOYMENT),
                "model_info": {"base_model": "azure/gpt-4o"},
            },
            True,
            id="model-info-base-model",
        ),
        pytest.param(
            {
                "model_name": "chat",
                "litellm_params": {**AZURE_DEPLOYMENT, "base_model": "azure/gpt-4o"},
            },
            False,
            id="litellm-params-base-model",
        ),
    ],
)
def test_seam_the_router_prices_base_model_only_from_model_info(
    monkeypatch, deployment, priced
):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    router = litellm.Router(model_list=[deployment])

    billed_input = _billed(router, "chat")[0] or 0

    assert (billed_input > 0) is priced


def test_seam_the_proxy_logger_carries_a_formatted_handler():
    handlers = logging.getLogger(log_output.LITELLM_PROXY_LOGGER).handlers

    assert handlers
    assert handlers[0].formatter is not None


def test_seam_json_logs_lift_a_json_message_into_top_level_fields():
    record = logging.LogRecord(
        "brownout",
        logging.WARNING,
        "events.py",
        1,
        "%s",
        ('{"event": "brownout_tier_crossed", "tier": 75}',),
        None,
    )

    line = json.loads(JsonFormatter().format(record))

    assert line["component"] == "brownout"
    assert line["level"] == "WARNING"
    assert line["event"] == "brownout_tier_crossed"
    assert line["tier"] == 75


def test_seam_an_explicit_coordination_redis_is_tried_before_the_environment():
    source = inspect.getsource(proxy_server.ProxyConfig)

    explicit = source.index("self._init_coordination_redis(config=config)")
    environment = source.index("self._init_coordination_redis_env_fallback(")

    assert explicit < environment


async def test_seam_without_redis_the_spend_counter_is_read_from_memory(monkeypatch):
    key = "spend:team:seam-memory"
    monkeypatch.setattr(proxy_server.spend_counter_cache, "redis_cache", None)
    proxy_server.spend_counter_cache.in_memory_cache.set_cache(key=key, value=0.3)

    try:
        spend = await proxy_server.get_current_spend(
            counter_key=key, fallback_spend=0.0
        )
    finally:
        proxy_server.spend_counter_cache.in_memory_cache.delete_cache(key=key)

    assert spend == 0.3


async def test_seam_fail_closed_refuses_a_memory_read_the_database_cannot_confirm(
    monkeypatch,
):
    key = "spend:team:seam-unverified"
    monkeypatch.setattr(proxy_server.spend_counter_cache, "redis_cache", None)
    monkeypatch.setattr(proxy_server, "prisma_client", None)
    monkeypatch.setitem(
        proxy_server.general_settings, "fail_closed_budget_enforcement", True
    )
    proxy_server.spend_counter_cache.in_memory_cache.set_cache(key=key, value=0.3)

    try:
        with pytest.raises(Exception, match="(?i)budget"):
            await proxy_server.get_current_spend(
                counter_key=key, fallback_spend=0.0, max_budget=10.0
            )
    finally:
        proxy_server.spend_counter_cache.in_memory_cache.delete_cache(key=key)
