from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from brownout.cli import gateway_file, main

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
