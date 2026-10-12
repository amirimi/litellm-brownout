from __future__ import annotations

import ast
import importlib.util
import inspect
import re
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Final

import litellm
import pytest
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils import litellm_logging
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth import auth_checks
from litellm.proxy.common_utils.timezone_utils import get_budget_reset_time
from litellm.proxy.spend_tracking import budget_reservation
from litellm.proxy.utils import _check_and_merge_model_level_guardrails

LITELLM_ROOT: Final = Path(importlib.util.find_spec("litellm").origin).parent
REFUSE_WHOLE_FROM: Final = (1, 101, 0)


def _installed_release() -> tuple[int, int, int]:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version("litellm"))
    assert match is not None, f"cannot read the LiteLLM release {version('litellm')!r}"
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


REFUSES_WHOLE: Final = _installed_release() >= REFUSE_WHOLE_FROM


def _function_in_source(
    relative_path: str, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse((LITELLM_ROOT / relative_path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == name
        ):
            return node
    raise AssertionError(f"{relative_path} no longer defines {name}")


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
    merge = source.index("self.data = _check_and_merge_model_level_guardrails(")
    hook = source.index("self.data = await proxy_logging_obj.pre_call_hook(")

    assert merge < hook


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
    check = _function_in_source("proxy/auth/auth_checks.py", "_team_max_budget_check")

    assert 'f"spend:team:{team_object.team_id}"' in ast.unparse(check).replace("'", '"')


def test_seam_the_spend_read_accepts_the_arguments_the_plugin_passes():
    read = _function_in_source("proxy/proxy_server.py", "get_current_spend")

    assert {"counter_key", "fallback_spend", "max_budget"} <= _parameter_names(read)


@pytest.mark.skipif(not REFUSES_WHOLE, reason="the hold is shrunk before 1.101.0")
def test_seam_fail_closed_refuses_a_worst_case_that_does_not_fit():
    policy = _function_in_source(
        "proxy/spend_tracking/budget_reservation.py",
        "_apply_over_budget_reservation_policy",
    )

    assert "fail_closed_budget_enforcement" in _parameter_names(policy)


@pytest.mark.skipif(REFUSES_WHOLE, reason="the hold is refused whole from 1.101.0")
def test_seam_before_1_101_an_over_budget_hold_is_shrunk():
    policy = _function_in_source(
        "proxy/spend_tracking/budget_reservation.py",
        "_apply_over_budget_reservation_policy",
    )

    assert "fail_closed_budget_enforcement" not in _parameter_names(policy)
    assert "_resize_applied_reservation(" in ast.unparse(policy)


@pytest.mark.parametrize(
    "key", ["fallbacks", "context_window_fallbacks", "content_policy_fallbacks"]
)
def test_seam_a_request_fallback_list_replaces_the_routers_own(key):
    source = (LITELLM_ROOT / "router.py").read_text(encoding="utf-8")

    assert f'kwargs.get("{key}", self.{key})' in source
    assert f'kwargs.pop("{key}", self.{key})' in source


def test_seam_key_and_team_router_settings_skip_keys_the_request_holds():
    source = (LITELLM_ROOT / "proxy/route_llm_request.py").read_text(encoding="utf-8")

    assert "if key in override_settings and key not in data:" in source


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
