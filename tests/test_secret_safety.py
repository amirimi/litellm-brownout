from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Final

import litellm
import pytest
from fastapi import HTTPException
from litellm.proxy._types import UserAPIKeyAuth

from brownout import bridge
from brownout.cli import main
from brownout.events import EventLog
from brownout.gateway import BrownoutHandler
from brownout.manifest import TeamPolicy

from .support import (
    CONFIG_DIR,
    SECRET,
    FakeSpendCounter,
    FixedSchedule,
    config_with_inline_secrets,
    disclosed,
    lab_gateway_config,
    manifest_for,
)

LAB_MANIFEST: Final = CONFIG_DIR / "manifest.yaml"
LAB_ENVIRONMENT: Final = "development"
REQUEST_SECRETS: Final = ("messages", "api_key")


@dataclass
class RecordingRedis:
    writes: list[tuple[str, Any]] = field(default_factory=list)

    async def async_set_cache(self, key: str, value: Any, nx: bool, ttl: int) -> bool:
        self.writes.append((key, value))
        return True


def secret_router() -> litellm.Router:
    model_list = copy.deepcopy(lab_gateway_config()["model_list"])
    for deployment in model_list:
        deployment["litellm_params"]["api_key"] = f"sk-{SECRET}"
    return litellm.Router(model_list=model_list)


def secret_handler(
    team: TeamPolicy, spend: float | Exception, redis: RecordingRedis
) -> BrownoutHandler:
    router: Final = secret_router()
    general: Final = {
        "master_key": f"sk-{SECRET}",
        "database_url": f"postgresql://llm:{SECRET}@db:5432/litellm",
        "fail_closed_budget_enforcement": True,
    }
    return BrownoutHandler(
        manifest=manifest_for(team),
        spend_counter=FakeSpendCounter(spend),
        permissions=bridge.LiteLLMPermissions(lambda: router),
        schedule=FixedSchedule(),
        guardrails=bridge.LiteLLMGuardrails(lambda: router),
        gateway=bridge.LiteLLMGatewayState(lambda: router, lambda: general),
        claims=bridge.RedisClaimStore(lambda: redis),
        events=EventLog(),
    )


def secret_auth(team: TeamPolicy, overrides: dict[str, Any]) -> UserAPIKeyAuth:
    values: dict[str, Any] = {
        "api_key": f"sk-{SECRET}",
        "token": f"{SECRET}-hashed",
        "key_alias": f"{SECRET}-alias",
        "user_id": f"{SECRET}-user",
        "team_id": team.team_id,
        "team_alias": team.team_id,
        "team_max_budget": team.envelope_usd,
        "team_spend": 0.0,
    }
    values.update(overrides)
    return UserAPIKeyAuth(**values)


def secret_request(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": f"the prompt holds {SECRET}"}],
        "api_key": f"sk-{SECRET}",
        "metadata": {
            "user_api_key": f"{SECRET}-hashed",
            "user_api_key_alias": f"{SECRET}-alias",
            "headers": {"authorization": f"Bearer sk-{SECRET}"},
        },
    }


def written_by_the_plugin(before: dict[str, Any], after: dict[str, Any]) -> Any:
    assert all(after[name] == before[name] for name in REQUEST_SECRETS)
    return {
        "request": _changed(before, after, ("metadata", *REQUEST_SECRETS)),
        "metadata": _changed(before["metadata"], after["metadata"], ()),
    }


def _changed(
    before: dict[str, Any], after: dict[str, Any], skipped: tuple[str, ...]
) -> dict[str, Any]:
    return {
        name: value
        for name, value in after.items()
        if name not in skipped and before.get(name) != value
    }


def brownout_log(caplog: pytest.LogCaptureFixture) -> str:
    formatter: Final = logging.Formatter("%(name)s %(message)s")
    return "\n".join(
        formatter.format(record)
        for record in caplog.records
        if record.name.startswith("brownout")
    )


@pytest.mark.parametrize(
    ("model", "spend", "overrides", "outcome"),
    [
        pytest.param("budget-lab-primary", 0.0, {}, "served", id="served"),
        pytest.param("budget-lab-primary", 950.0, {}, "degraded", id="degraded"),
        pytest.param(
            "budget-lab-primary",
            950.0,
            {"models": ["budget-lab-primary"]},
            "rejected",
            id="fallback-rejected",
        ),
        pytest.param("made-up-model", 0.0, {}, "refused", id="unknown-model"),
        pytest.param(
            "budget-lab-primary",
            RuntimeError("counter unavailable"),
            {},
            "refused",
            id="counter-unreadable",
        ),
        pytest.param(
            "budget-lab-primary",
            0.0,
            {"max_budget": 5.0},
            "refused",
            id="key-with-a-budget",
        ),
    ],
)
async def test_the_handler_writes_no_secret_it_can_see(
    team, caplog, monkeypatch, model, spend, overrides, outcome
):
    monkeypatch.setenv("LITELLM_ENVIRONMENT", LAB_ENVIRONMENT)
    caplog.set_level(logging.DEBUG)
    redis: Final = RecordingRedis()
    handler: Final = secret_handler(team, spend, redis)
    data: Final = secret_request(model)
    before: Final = copy.deepcopy(data)
    outputs: list[Any] = []

    try:
        await handler.async_pre_call_hook(
            user_api_key_dict=secret_auth(team, overrides),
            cache=None,
            data=data,
            call_type="acompletion",
        )
    except HTTPException as error:
        outputs.append({"detail": error.detail, "headers": error.headers})
    outputs.append(
        await handler.async_post_call_response_headers_hook(
            data=data,
            user_api_key_dict=secret_auth(team, overrides),
            response=object(),
            litellm_call_info={
                "model_id": "deployment-1",
                "api_key": f"sk-{SECRET}",
                "api_base": f"https://{SECRET}.example",
            },
        )
    )
    outputs.append(written_by_the_plugin(before, data))
    outputs.append(redis.writes)

    assert disclosed(data)["outcome"] == outcome
    assert SECRET not in json.dumps(outputs, default=str)
    assert SECRET not in brownout_log(caplog)


def test_check_prints_no_secret_from_the_gateway_config(tmp_path, capsys):
    exit_code = main(
        [
            "check",
            "--gateway-config",
            str(config_with_inline_secrets(tmp_path)),
            "--manifest",
            str(LAB_MANIFEST),
        ]
    )

    printed = capsys.readouterr()
    assert exit_code == 1
    assert SECRET not in printed.out + printed.err


def test_check_reports_an_unreadable_config_by_position_only(tmp_path, capsys):
    config: Final = tmp_path / "config.yaml"
    config.write_text(
        f'general_settings:\n  master_key: "sk-{SECRET}\n', encoding="utf-8"
    )

    exit_code = main(
        ["check", "--gateway-config", str(config), "--manifest", str(LAB_MANIFEST)]
    )

    error = capsys.readouterr().err
    assert exit_code == 2
    assert f"{config} is not valid YAML at line 3" in error
    assert SECRET not in error
