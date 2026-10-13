from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import yaml

from brownout.cli import InitOptions, gateway_file, init, main
from brownout.manifest import Manifest

from .support import CONFIG_DIR, lab_gateway_config

LAB_CONFIG = CONFIG_DIR / "config.yaml"
LAB_MANIFEST = CONFIG_DIR / "manifest.yaml"


def write(path: Path, document: Any) -> Path:
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_the_lab_passes_the_check(capsys):
    assert (
        main(
            [
                "check",
                "--gateway-config",
                str(LAB_CONFIG),
                "--manifest",
                str(LAB_MANIFEST),
            ]
        )
        == 0
    )
    assert "brownout check passed on LiteLLM" in capsys.readouterr().out


def test_a_broken_manifest_fails_with_its_rule_id(tmp_path, capsys):
    manifest = yaml.safe_load(LAB_MANIFEST.read_text(encoding="utf-8"))
    manifest["teams"]["budget-lab"]["budget_duration"] = "2d"

    exit_code = main(
        [
            "check",
            "--gateway-config",
            str(LAB_CONFIG),
            "--manifest",
            str(write(tmp_path / "manifest.yaml", manifest)),
        ]
    )

    assert exit_code == 1
    assert capsys.readouterr().out.startswith("duration-tumbles\t")


def check_with_general_settings(tmp_path, extra: dict[str, Any]) -> int:
    config = dict(lab_gateway_config())
    config["general_settings"] = {**config["general_settings"], **extra}
    return main(
        [
            "check",
            "--gateway-config",
            str(write(tmp_path / "config.yaml", config)),
            "--manifest",
            str(LAB_MANIFEST),
        ]
    )


@pytest.mark.parametrize(
    "alerting",
    [
        {"alerting": ["slack"]},
        {"alerting": ["slack"], "alert_types": ["llm_exceptions", "budget_alerts"]},
    ],
)
def test_litellm_budget_alerts_fail_with_their_rule_id(tmp_path, capsys, alerting):
    exit_code = check_with_general_settings(tmp_path, alerting)

    assert exit_code == 1
    assert "no-native-budget-alerts\tgeneral_settings.alert_types" in (
        capsys.readouterr().out
    )


def test_litellm_alerting_without_budget_alerts_passes(tmp_path):
    exit_code = check_with_general_settings(
        tmp_path,
        {"alerting": ["slack"], "alert_types": ["llm_exceptions", "outage_alerts"]},
    )

    assert exit_code == 0


def test_a_deployment_without_explicit_prices_is_priced_from_the_cost_map():
    gateway = gateway_file(
        {
            "model_list": [
                {
                    "model_name": "mapped",
                    "litellm_params": {"model": "openai/gpt-4o"},
                    "model_info": {"data_class": "internal"},
                }
            ]
        }
    )

    mapped = gateway.groups["mapped"]
    assert mapped.input_cost_per_token is not None
    assert mapped.output_cost_per_token is not None


def test_a_group_is_unpriced_when_any_deployment_is():
    gateway = gateway_file(
        {
            "model_list": [
                {
                    "model_name": "split",
                    "litellm_params": {
                        "model": "openai/mock-a",
                        "input_cost_per_token": 0.001,
                        "output_cost_per_token": 0.002,
                    },
                },
                {
                    "model_name": "split",
                    "litellm_params": {"model": "openai/never-mapped"},
                },
            ]
        }
    )

    assert gateway.groups["split"].input_cost_per_token is None


def test_every_lab_deployment_declares_its_output_ceiling():
    for deployment in lab_gateway_config()["model_list"]:
        ceiling = deployment["model_info"].get("max_output_tokens")
        assert isinstance(ceiling, int) and ceiling > 0, (
            f"deployment {deployment['model_name']} declares no max_output_tokens, so "
            "LiteLLM reserves 16384 output tokens for every request on it"
        )


DEFAULT_ALERTS: Final = ("llm_exceptions", "budget_alerts")
BUDGETED_TEAMS: Final = [
    {"team_id": "budget-lab", "max_budget": 1.0, "budget_duration": "7d", "models": []},
    {"team_id": "search", "max_budget": 500.0, "budget_duration": "7d", "models": []},
]
TEAM_KEY: Final = {
    "token": "hash-a",
    "key_alias": "search-app",
    "team_id": "search",
    "max_budget": None,
    "model_max_budget": {},
    "budget_limits": None,
}


def fake_gateway(
    teams: list[dict[str, Any]] | None = None,
    keys: list[dict[str, Any]] | None = None,
    models: list[dict[str, Any]] | None = None,
    status: int = 200,
) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error": "unavailable"})
        if request.url.path == "/team/list":
            return httpx.Response(
                200, json=teams if teams is not None else BUDGETED_TEAMS
            )
        if request.url.path == "/key/list":
            return httpx.Response(
                200,
                json={
                    "keys": keys if keys is not None else [TEAM_KEY],
                    "total_count": 1,
                    "total_pages": 1,
                    "current_page": 1,
                },
            )
        return httpx.Response(200, json={"data": models or []})

    return httpx.Client(
        base_url="http://gateway.test", transport=httpx.MockTransport(handle)
    )


def options(tmp_path: Path, **overrides: Any) -> InitOptions:
    values: dict[str, Any] = {
        "gateway": "http://gateway.test",
        "gateway_config": LAB_CONFIG,
        "manifest": None,
        "manifest_out": tmp_path / "brownout.yaml",
        "config_out": tmp_path / "brownout.config.yaml",
        "write": True,
    }
    values.update(overrides)
    return InitOptions(**values)


def test_init_writes_a_manifest_and_config_that_pass_the_check(tmp_path, capsys):
    chosen = options(tmp_path)

    exit_code = init(chosen, fake_gateway(), DEFAULT_ALERTS)

    assert exit_code == 0
    manifest = Manifest.from_file(chosen.manifest_out)
    assert manifest.team_ids == ("budget-lab", "search")
    assert (
        main(
            [
                "check",
                "--gateway-config",
                str(chosen.config_out),
                "--manifest",
                str(chosen.manifest_out),
            ]
        )
        == 0
    )
    assert "team-in-manifest\tmaster key" in capsys.readouterr().out


def test_a_dry_run_writes_nothing_and_still_reports(tmp_path, capsys):
    chosen = options(tmp_path, write=False)

    exit_code = init(
        chosen,
        fake_gateway(
            teams=[
                *BUDGETED_TEAMS,
                {
                    "team_id": "loose",
                    "max_budget": None,
                    "budget_duration": None,
                    "models": [],
                },
            ]
        ),
        DEFAULT_ALERTS,
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert not chosen.manifest_out.exists() and not chosen.config_out.exists()
    assert "+++ " in output
    assert "team-in-manifest\tloose\t" in output
    assert "<YOUR BUDGET>" in output


@pytest.mark.parametrize("taken", ["manifest_out", "config_out"])
def test_init_never_overwrites_an_existing_output(tmp_path, taken):
    chosen = options(tmp_path)
    outputs = {"manifest_out": chosen.manifest_out, "config_out": chosen.config_out}
    outputs[taken].write_text("keep me\n", encoding="utf-8")

    assert init(chosen, fake_gateway(), DEFAULT_ALERTS) == 2
    assert outputs[taken].read_text(encoding="utf-8") == "keep me\n"


def test_an_output_that_is_an_input_is_refused(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(LAB_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")

    assert (
        init(
            options(tmp_path, gateway_config=config, config_out=config),
            fake_gateway(),
            DEFAULT_ALERTS,
        )
        == 2
    )


def test_an_invalid_existing_manifest_stops_init_with_its_rule(tmp_path, capsys):
    broken = write(tmp_path / "theirs.yaml", {"version": "v1", "teams": {}})

    assert init(options(tmp_path, manifest=broken), fake_gateway(), DEFAULT_ALERTS) == 2
    assert "manifest-schema" in capsys.readouterr().err


def test_merging_keeps_their_manifest_and_appends_missing_teams(tmp_path):
    original = LAB_MANIFEST.read_text(encoding="utf-8")
    chosen = options(tmp_path, manifest=LAB_MANIFEST)

    init(chosen, fake_gateway(), DEFAULT_ALERTS)

    merged = chosen.manifest_out.read_text(encoding="utf-8")
    assert merged.startswith(original)
    assert Manifest.from_file(chosen.manifest_out).team_policy("search") is not None


def test_an_unreadable_gateway_exits_two(tmp_path, capsys):
    assert init(options(tmp_path), fake_gateway(status=401), DEFAULT_ALERTS) == 2
    assert "gateway" in capsys.readouterr().err


def test_a_gateway_with_nothing_to_govern_writes_no_manifest(tmp_path, capsys):
    chosen = options(tmp_path)

    exit_code = init(
        chosen,
        fake_gateway(
            teams=[
                {
                    "team_id": "loose",
                    "max_budget": None,
                    "budget_duration": None,
                    "models": [],
                }
            ]
        ),
        DEFAULT_ALERTS,
    )

    assert exit_code == 1
    assert not chosen.manifest_out.exists()
    assert "team-in-manifest\tmanifest\t" in capsys.readouterr().out


def test_init_without_a_master_key_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)

    exit_code = main(
        [
            "init",
            "--gateway",
            "http://gateway.test",
            "--gateway-config",
            str(LAB_CONFIG),
            "--manifest-out",
            str(tmp_path / "brownout.yaml"),
            "--config-out",
            str(tmp_path / "brownout.config.yaml"),
        ]
    )

    assert exit_code == 2
    assert "LITELLM_MASTER_KEY" in capsys.readouterr().err
