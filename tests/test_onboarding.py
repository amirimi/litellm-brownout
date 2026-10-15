from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from brownout.gateway_api import LiteLLMKey, LiteLLMTeam
from brownout.gateway_config import GatewayConfig, read_gateway_config
from brownout.manifest import Manifest
from brownout.onboarding import (
    AppendSetting,
    Finding,
    RemoveSetting,
    SetSetting,
    TeamEntry,
    admin_keys_notice,
    config_plan,
    describe_change,
    exit_code,
    key_findings,
    model_findings,
    nothing_to_govern,
    reconcile_findings,
    team_entries,
    team_findings,
    violation_findings,
)
from brownout.refusals import RuleId, Violation
from brownout.rules import ModelGroup

GATEWAY: Final = "http://gateway.test"
READY_CONFIG: Final[dict[str, Any]] = {
    "general_settings": {
        "fail_closed_budget_enforcement": True,
        "coordination_redis": {"host": "redis", "port": 6379},
    },
    "litellm_settings": {
        "callbacks": ["brownout.gateway.handler"],
        "budget_exceeded_status_code": 429,
    },
}


def planned(config: dict[str, Any]) -> GatewayConfig:
    return read_gateway_config(Path("config.yaml"), config)


def team(
    team_id: str,
    max_budget: float | None = 500.0,
    duration: str | None = "7d",
    models: tuple[str, ...] = (),
) -> LiteLLMTeam:
    return LiteLLMTeam(team_id, max_budget, duration, models)


def manifest_with(teams: dict[str, Any]) -> Manifest:
    return Manifest.from_document(
        "test",
        {
            "data_class_order": ["internal"],
            "teams": teams,
        },
    )


def json_body(command: str) -> str:
    return command.split(" -d '", 1)[1].rstrip("'")


def test_a_budgeted_team_becomes_an_entry_that_matches_litellm():
    entries = team_entries((team("search"), team("legal", 2000.0, "30d")), frozenset())

    assert entries == (
        TeamEntry("legal", 2000.0, "30d"),
        TeamEntry("search", 500.0, "7d"),
    )
    assert entries[1].document() == {
        "envelope_usd": 500.0,
        "budget_duration": "7d",
        "warn_tiers": [75, 90],
        "routes": {},
    }


def test_entries_skip_teams_the_manifest_already_has_and_teams_it_cannot_govern():
    entries = team_entries(
        (
            team("search"),
            team("kept"),
            team("unbudgeted", None),
            team("zero", 0.0),
            team("biweekly", 100.0, "2d"),
            team("windowless", 100.0, None),
        ),
        frozenset({"kept"}),
    )

    assert [entry.team_id for entry in entries] == ["search"]


def test_a_team_without_a_budget_is_a_finding_with_a_placeholder_command():
    findings = team_findings((team("unbudgeted", None),), GATEWAY)

    assert [(f.rule, f.subject, f.blocking) for f in findings] == [
        (RuleId.TEAM_IN_MANIFEST, "unbudgeted", True)
    ]
    command = findings[0].resolution[0]
    assert command.startswith(f"curl -X POST {GATEWAY}/team/update ")
    assert "Bearer $LITELLM_MASTER_KEY" in command
    assert "<YOUR BUDGET>" in command
    with pytest.raises(json.JSONDecodeError):
        json.loads(json_body(command))


def test_a_team_with_a_rejected_duration_is_a_tumbles_finding():
    findings = team_findings((team("biweekly", 100.0, "2d"),), GATEWAY)

    assert [(f.rule, f.subject) for f in findings] == [
        (RuleId.DURATION_TUMBLES, "biweekly")
    ]
    command = findings[0].resolution[0]
    assert json.loads(json_body(command))["budget_duration"] == (
        "<YOUR DURATION, SUCH AS 7d>"
    )


def test_teamless_and_self_budgeted_keys_are_findings_with_runnable_commands():
    findings = key_findings(
        (
            LiteLLMKey("hash-a", "app", "search", ()),
            LiteLLMKey("hash-b", None, None, ()),
            LiteLLMKey("hash-c", "capped", "search", ("max_budget",)),
        ),
        GATEWAY,
        "hash-init",
    )

    assert [(f.rule, f.subject) for f in findings] == [
        (RuleId.TEAM_IN_MANIFEST, "hash-b"),
        (RuleId.KEY_HAS_NO_BUDGET, "capped"),
    ]
    assert json.loads(json_body(findings[0].resolution[0])) == {
        "key": "hash-b",
        "team_id": "<YOUR TEAM ID>",
    }
    assert json.loads(json_body(findings[1].resolution[0])) == {
        "key": "hash-c",
        "max_budget": None,
        "model_max_budget": {},
        "budget_limits": None,
    }


def test_an_unpriced_model_is_a_finding_with_the_lines_to_add():
    findings = model_findings(
        {
            "chat": ModelGroup("chat", 1e-6, 2e-6, ()),
            "mystery": ModelGroup("mystery", None, None, ()),
        }
    )

    assert [(f.rule, f.subject) for f in findings] == [(RuleId.MODEL_PRICED, "mystery")]
    assert any("input_cost_per_token" in line for line in findings[0].resolution)
    assert "restart the gateway" in findings[0].detail
    assert "admin UI" in findings[0].detail


def test_reconciling_names_every_mismatch_with_litellm():
    manifest = manifest_with(
        {
            "search": {
                "envelope_usd": 400.0,
                "budget_duration": "30d",
                "routes": {
                    "chat": {"fallback": "chat-cheap", "degrade_at": 90},
                },
            },
            "ghost": {
                "envelope_usd": 10.0,
                "budget_duration": "7d",
            },
        }
    )

    findings = reconcile_findings(
        manifest, (team("search", 500.0, "7d", ("chat",)),), GATEWAY
    )

    assert sorted((f.rule, f.subject) for f in findings) == sorted(
        [
            (RuleId.CEILING_MATCHES_ENVELOPE, "search"),
            (RuleId.DURATION_MATCHES, "search"),
            (RuleId.FALLBACK_IN_TEAM_MODELS, "search/chat"),
            (RuleId.TEAM_EXISTS, "ghost"),
        ]
    )
    assert all(len(finding.resolution) == 2 for finding in findings)


@pytest.mark.parametrize("models", [(), ("all-proxy-models",)])
def test_a_team_that_allows_every_model_allows_its_fallbacks(models):
    manifest = manifest_with(
        {
            "search": {
                "envelope_usd": 500.0,
                "budget_duration": "7d",
                "routes": {"chat": {"fallback": "chat-cheap", "degrade_at": 90}},
            }
        }
    )

    assert reconcile_findings(manifest, (team("search", models=models),), GATEWAY) == []


def test_a_ready_config_needs_no_change():
    assert config_plan(planned(READY_CONFIG)) == ()


def test_a_vanilla_config_gets_every_mechanical_fix():
    config: dict[str, Any] = {
        "general_settings": {
            "alerting": ["slack"],
            "disable_budget_reservation": True,
        },
        "litellm_settings": {
            "callbacks": ["langfuse"],
            "cache_params": {
                "type": "redis",
                "host": "os.environ/REDIS_HOST",
                "port": 6379,
                "password": "os.environ/REDIS_PASSWORD",
                "ttl": 600,
            },
        },
    }

    changes = config_plan(planned(config))

    assert [type(change) for change in changes] == [
        AppendSetting,
        SetSetting,
        RemoveSetting,
        SetSetting,
        SetSetting,
    ]
    by_key = {change.key: change for change in changes}
    assert by_key["callbacks"] == AppendSetting(
        "litellm_settings", "callbacks", "brownout.gateway.handler"
    )
    assert by_key["fail_closed_budget_enforcement"].value is True
    assert "every budget on the gateway" in (
        by_key["fail_closed_budget_enforcement"].note or ""
    )
    assert "alert_types" not in by_key
    assert by_key["budget_exceeded_status_code"].value == 429
    assert by_key["coordination_redis"].value == {
        "host": "os.environ/REDIS_HOST",
        "port": 6379,
        "password": "os.environ/REDIS_PASSWORD",
    }


@pytest.mark.parametrize(
    "alerting",
    [
        pytest.param({"alerting": ["slack"]}, id="default-types"),
        pytest.param(
            {"alerting": ["slack"], "alert_types": ["outage_alerts", "budget_alerts"]},
            id="budget-alerts",
        ),
    ],
)
def test_init_leaves_alerting_as_written(alerting):
    config = {
        **READY_CONFIG,
        "general_settings": {**READY_CONFIG["general_settings"], **alerting},
    }

    assert config_plan(planned(config)) == ()


@pytest.mark.parametrize(
    "cache_params",
    [None, {"type": "redis"}, {"type": "local", "host": "redis"}],
)
def test_no_coordination_redis_is_derived_without_a_redis_target(cache_params):
    config = {
        "general_settings": {"fail_closed_budget_enforcement": True},
        "litellm_settings": {
            "callbacks": ["brownout.gateway.handler"],
            "budget_exceeded_status_code": 429,
            "cache_params": cache_params,
        },
    }

    assert config_plan(planned(config)) == ()


def test_a_missing_redis_violation_carries_the_block_to_add():
    findings = violation_findings(
        [
            Violation(
                RuleId.REDIS_CONFIGURED,
                "general_settings.coordination_redis",
                "is unset",
            )
        ]
    )

    assert findings[0].blocking is True
    assert any("<YOUR REDIS HOST>" in line for line in findings[0].resolution)


def test_only_the_admin_keys_notice_leaves_init_ready():
    blocking = Finding(RuleId.MODEL_PRICED, "mystery", "has no price", (), True)

    assert admin_keys_notice(True).blocking is False
    assert exit_code([admin_keys_notice(True)]) == 0
    assert exit_code([admin_keys_notice(True), blocking]) == 1


@pytest.mark.parametrize(
    ("own_key_teamless", "subject", "detail"),
    [
        (
            True,
            "admin keys",
            (
                "the master key and the key init runs with have no team, so model "
                "calls made with them will be refused. Send model traffic with team keys"
            ),
        ),
        (
            False,
            "master key",
            (
                "the master key has no team, so model calls made with it will be "
                "refused. Send model traffic with team keys"
            ),
        ),
    ],
)
def test_the_admin_keys_notice_names_the_init_key_only_when_it_has_no_team(
    own_key_teamless, subject, detail
):
    notice = admin_keys_notice(own_key_teamless)

    assert (notice.subject, notice.detail) == (subject, detail)


@pytest.mark.parametrize(
    ("change", "described"),
    [
        (
            AppendSetting("litellm_settings", "callbacks", "brownout.gateway.handler"),
            "add brownout.gateway.handler to litellm_settings.callbacks",
        ),
        (
            RemoveSetting("general_settings", "disable_budget_reservation"),
            "remove general_settings.disable_budget_reservation",
        ),
        (
            SetSetting(
                "general_settings", "fail_closed_budget_enforcement", True, None
            ),
            "set general_settings.fail_closed_budget_enforcement to true",
        ),
        (
            SetSetting("litellm_settings", "budget_exceeded_status_code", 429, None),
            "set litellm_settings.budget_exceeded_status_code to 429",
        ),
        (
            SetSetting(
                "general_settings",
                "coordination_redis",
                {"host": "redis.internal", "password": "hunter2"},
                None,
            ),
            "set general_settings.coordination_redis with host, password",
        ),
    ],
)
def test_a_change_is_described_without_values_read_from_the_config(change, described):
    assert describe_change(change) == described


def test_the_key_init_runs_with_is_left_to_the_admin_keys_notice():
    own = LiteLLMKey("hash-init", None, None, ())
    other = LiteLLMKey("hash-other", None, None, ())

    findings = key_findings((own, other), GATEWAY, "hash-init")

    assert [finding.subject for finding in findings] == ["hash-other"]


def test_the_key_init_runs_with_still_answers_for_its_own_budget():
    own = LiteLLMKey("hash-init", "init", None, ("max_budget",))

    findings = key_findings((own,), GATEWAY, "hash-init")

    assert [(f.rule, f.subject) for f in findings] == [
        (RuleId.KEY_HAS_NO_BUDGET, "init")
    ]


def test_a_gateway_without_teams_is_told_how_to_create_its_first_one():
    finding = nothing_to_govern(GATEWAY, teams_exist=False)

    team_command, key_command = finding.resolution
    assert team_command.startswith(f"curl -X POST {GATEWAY}/team/new ")
    assert '"team_id": "<YOUR TEAM ID>"' in team_command
    assert '"max_budget": <YOUR BUDGET>' in team_command
    assert key_command.startswith(f"curl -X POST {GATEWAY}/key/generate ")
    assert '"team_id": "<YOUR TEAM ID>"' in key_command
    assert finding.blocking is True


def test_teams_without_budgets_are_resolved_through_their_own_findings():
    assert nothing_to_govern(GATEWAY, teams_exist=True).resolution == ()


def test_a_member_budget_inside_a_team_is_a_finding_with_its_clearing_command():
    budgeted = LiteLLMTeam("search", 500.0, "7d", (), ("alice",))

    findings = [
        finding
        for finding in team_findings((budgeted,), GATEWAY)
        if finding.rule is RuleId.MEMBER_HAS_NO_BUDGET
    ]

    assert [(f.subject, f.blocking) for f in findings] == [("search/alice", True)]
    assert json.loads(json_body(findings[0].resolution[0])) == {
        "team_id": "search",
        "user_id": "alice",
        "max_budget_in_team": None,
    }
    assert (
        findings[0]
        .resolution[0]
        .startswith(f"curl -X POST {GATEWAY}/team/member_update ")
    )
