from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import yaml

from brownout import plugin_folder
from brownout.cli import InitOptions, check, init, main
from brownout.gateway_api import read_model_groups
from brownout.inputs import DocumentError
from brownout.manifest import Manifest

from .support import (
    CONFIG_DIR,
    SECRET,
    config_with_inline_secrets,
    gateway_file_of,
    lab_gateway_config,
)

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
        pytest.param({"alerting": ["slack"]}, id="default-types"),
        pytest.param(
            {"alerting": ["slack"], "alert_types": ["llm_exceptions", "budget_alerts"]},
            id="budget-alerts",
        ),
        pytest.param(
            {"alerting": ["slack"], "alert_types": ["outage_alerts"]},
            id="no-budget-alerts",
        ),
    ],
)
def test_litellm_alerting_passes_the_check_with_or_without_budget_alerts(
    tmp_path, alerting
):
    assert check_with_general_settings(tmp_path, alerting) == 0


def test_a_deployment_without_explicit_prices_is_priced_from_the_cost_map():
    gateway = gateway_file_of(
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


def test_a_deployment_priced_in_model_info_is_priced_as_litellm_bills_it():
    gateway = gateway_file_of(
        {
            "model_list": [
                {
                    "model_name": "listed",
                    "litellm_params": {"model": "openai/never-mapped"},
                    "model_info": {
                        "input_cost_per_token": 3e-6,
                        "output_cost_per_token": 4e-6,
                    },
                }
            ]
        }
    )

    listed = gateway.groups["listed"]
    assert (listed.input_cost_per_token, listed.output_cost_per_token) == (3e-6, 4e-6)


def test_the_lab_passes_with_its_prices_moved_into_model_info(tmp_path):
    config = lab_gateway_config()
    for deployment in config["model_list"]:
        params = deployment["litellm_params"]
        moved = {
            key: params.pop(key)
            for key in ("input_cost_per_token", "output_cost_per_token")
            if key in params
        }
        deployment["model_info"] = {**deployment.get("model_info", {}), **moved}
    config_path = write(tmp_path / "config.yaml", config)

    assert check(config_path, LAB_MANIFEST, {}) == []


AZURE_PARAMS: Final = {
    "model": "azure/my-custom-deployment",
    "api_key": "sk-azure",
    "api_base": "https://lab.openai.azure.com",
    "api_version": "2024-06-01",
}


def test_an_azure_deployment_priced_through_model_info_base_model_is_priced():
    gateway = gateway_file_of(
        {
            "model_list": [
                {
                    "model_name": "chat",
                    "litellm_params": dict(AZURE_PARAMS),
                    "model_info": {"base_model": "azure/gpt-4o"},
                }
            ]
        }
    )

    assert gateway.groups["chat"].input_cost_per_token is not None
    assert gateway.groups["chat"].output_cost_per_token is not None


def test_a_base_model_in_litellm_params_leaves_the_group_unpriced():
    gateway = gateway_file_of(
        {
            "model_list": [
                {
                    "model_name": "chat",
                    "litellm_params": {**AZURE_PARAMS, "base_model": "azure/gpt-4o"},
                }
            ]
        }
    )

    assert gateway.groups["chat"].input_cost_per_token is None


def test_a_group_is_unpriced_when_any_deployment_is():
    gateway = gateway_file_of(
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


INIT_TOKEN: Final = "hash-init"
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
    methods: list[str] | None = None,
) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        if methods is not None:
            methods.append(request.method)
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


def lab_config_copy(directory: Path) -> Path:
    copy = directory / "config.yaml"
    copy.write_text(LAB_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return copy


def options(tmp_path: Path, **overrides: Any) -> InitOptions:
    values: dict[str, Any] = {
        "gateway": "http://gateway.test",
        "manifest": None,
        "manifest_out": tmp_path / "brownout.yaml",
        "plugin_dir": tmp_path / "plugins",
        "platform": "compose",
        "service": None,
        "compose_dir": tmp_path,
        "compose_file": None,
        "redis_host": None,
        "write": True,
    }
    values.update(overrides)
    if "gateway_config" not in values:
        values["gateway_config"] = lab_config_copy(tmp_path)
    if "config_out" not in values:
        values["config_out"] = values["gateway_config"]
    return InitOptions(**values)


def compose_json(services: dict[str, str | None]) -> str:
    return json.dumps(
        {
            "services": {
                name: {"image": image, "environment": {}, "volumes": []}
                for name, image in services.items()
            }
        }
    )


def fake_compose(services: dict[str, str | None]):
    def run(arguments, directory):
        return 0, compose_json(services)

    return run


def test_init_writes_a_manifest_and_config_that_pass_the_check(tmp_path, capsys):
    chosen = options(tmp_path)

    exit_code = init(chosen, fake_gateway(), INIT_TOKEN)

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
        INIT_TOKEN,
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert not chosen.manifest_out.exists()
    assert chosen.config_out.read_text(encoding="utf-8") == LAB_CONFIG.read_text(
        encoding="utf-8"
    )
    assert "+++ " in output
    assert "team-in-manifest\tloose\t" in output
    assert "<YOUR BUDGET>" in output


@pytest.mark.parametrize("taken", ["manifest_out", "config_out"])
def test_init_never_overwrites_an_existing_output(tmp_path, taken):
    chosen = options(tmp_path, config_out=tmp_path / "other.config.yaml")
    outputs = {"manifest_out": chosen.manifest_out, "config_out": chosen.config_out}
    outputs[taken].write_text("keep me\n", encoding="utf-8")

    assert init(chosen, fake_gateway(), INIT_TOKEN) == 2
    assert outputs[taken].read_text(encoding="utf-8") == "keep me\n"


def test_an_output_that_is_an_input_is_refused(tmp_path):
    theirs = tmp_path / "theirs.yaml"
    theirs.write_text(LAB_MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")

    assert (
        init(
            options(tmp_path, manifest=theirs, manifest_out=theirs),
            fake_gateway(),
            INIT_TOKEN,
        )
        == 2
    )


def test_init_edits_the_config_in_place_and_keeps_its_comments(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(
        "# the team's gateway\n" + LAB_CONFIG.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    chosen = options(tmp_path, gateway_config=config)

    exit_code = init(chosen, fake_gateway(), INIT_TOKEN)

    written = config.read_text(encoding="utf-8")
    assert exit_code == 0
    assert written.startswith("# the team's gateway\n")
    assert check_files(config, chosen.manifest_out) == 0
    assert "brownout check passed on LiteLLM" in capsys.readouterr().out


def test_a_separate_config_out_is_written_even_without_changes(tmp_path):
    chosen = options(tmp_path, config_out=tmp_path / "copy.yaml")

    init(chosen, fake_gateway(), INIT_TOKEN)

    assert chosen.config_out.read_text(encoding="utf-8") == LAB_CONFIG.read_text(
        encoding="utf-8"
    )


def test_init_writes_the_plugin_folder_beside_the_manifest(tmp_path, capsys):
    chosen = options(tmp_path)

    init(chosen, fake_gateway(), INIT_TOKEN)

    assert (chosen.plugin_dir / "brownout" / "gateway.py").exists()
    assert f"wrote {chosen.plugin_dir / 'brownout'}" in capsys.readouterr().out


def test_init_refuses_a_plugin_folder_in_the_configs_directory(tmp_path, capsys):
    chosen = options(tmp_path, plugin_dir=tmp_path)
    before = chosen.gateway_config.read_text(encoding="utf-8")

    exit_code = init(chosen, fake_gateway(), INIT_TOKEN)

    assert exit_code == 2
    assert chosen.gateway_config.read_text(encoding="utf-8") == before
    assert not chosen.manifest_out.exists()
    assert "loads brownout/gateway.py by file path" in capsys.readouterr().err


def test_a_named_redis_host_becomes_the_coordination_redis(tmp_path):
    config = lab_gateway_config()
    config["general_settings"].pop("coordination_redis", None)
    config.get("litellm_settings", {}).pop("cache_params", None)
    chosen = options(
        tmp_path,
        gateway_config=write(tmp_path / "config.yaml", config),
        redis_host="redis",
    )

    init(chosen, fake_gateway(), INIT_TOKEN)

    written = yaml.safe_load(chosen.config_out.read_text(encoding="utf-8"))
    assert written["general_settings"]["coordination_redis"] == {
        "host": "redis",
        "port": 6379,
        "password": "os.environ/REDIS_PASSWORD",
    }


def test_init_writes_the_override_for_the_litellm_service(tmp_path, capsys):
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    chosen = options(tmp_path)

    init(
        chosen,
        fake_gateway(),
        INIT_TOKEN,
        fake_compose(
            {"proxy": "ghcr.io/berriai/litellm:main-v1.106.2", "db": "postgres:16"}
        ),
    )

    override = yaml.safe_load((tmp_path / "compose.override.yaml").read_text("utf-8"))
    assert list(override["services"]) == ["proxy"]
    printed = capsys.readouterr().out
    assert "wrote " + str(tmp_path / "compose.override.yaml") in printed
    assert "docker compose up -d" in printed


def test_init_writes_nothing_when_it_cannot_pick_the_service(tmp_path, capsys):
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    chosen = options(tmp_path)
    before = chosen.gateway_config.read_text(encoding="utf-8")

    exit_code = init(
        chosen,
        fake_gateway(),
        INIT_TOKEN,
        fake_compose(
            {"a": "litellm/litellm:v1.106.2", "b": "litellm/litellm:v1.106.2"}
        ),
    )

    assert exit_code == 2
    assert chosen.gateway_config.read_text(encoding="utf-8") == before
    assert not chosen.manifest_out.exists() and not chosen.plugin_dir.exists()
    assert "several services run LiteLLM's image: a, b" in capsys.readouterr().err


def test_an_existing_override_gets_the_lines_to_merge_and_stays_untouched(
    tmp_path, capsys
):
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "compose.override.yaml").write_text("services: {}\n", encoding="utf-8")

    init(
        options(tmp_path),
        fake_gateway(),
        INIT_TOKEN,
        fake_compose({"proxy": "litellm/litellm:v1"}),
    )

    assert (tmp_path / "compose.override.yaml").read_text("utf-8") == "services: {}\n"
    printed = capsys.readouterr()
    assert "compose.override.yaml already exists" in printed.err
    assert "/app/plugins" in printed.out


def test_without_a_compose_file_init_prints_docker_run_flags(tmp_path, capsys):
    chosen = options(tmp_path)

    init(chosen, fake_gateway(), INIT_TOKEN)

    assert (
        f"-v {chosen.plugin_dir.resolve()}:/app/plugins:ro" in capsys.readouterr().out
    )


def test_for_helm_init_leaves_the_config_and_prints_the_chart_values(tmp_path, capsys):
    config = lab_gateway_config()
    config["litellm_settings"].pop("callbacks")
    chosen = options(
        tmp_path,
        platform="helm",
        gateway_config=write(tmp_path / "config.yaml", config),
    )
    before = chosen.gateway_config.read_text(encoding="utf-8")

    init(chosen, fake_gateway(), INIT_TOKEN)

    printed = capsys.readouterr().out
    assert chosen.gateway_config.read_text(encoding="utf-8") == before
    assert chosen.manifest_out.exists()
    assert not chosen.plugin_dir.exists()
    assert "extraInitContainers:" in printed
    assert f"litellm-brownout=={plugin_folder.running_version()}" in printed
    assert "- brownout.gateway.handler" in printed
    assert "kubectl create configmap brownout-manifest" in printed


def test_an_invalid_existing_manifest_stops_init_with_its_rule(tmp_path, capsys):
    broken = write(tmp_path / "theirs.yaml", {"teams": {}})

    assert (
        init(
            options(tmp_path, manifest=broken),
            fake_gateway(),
            INIT_TOKEN,
        )
        == 2
    )
    assert "manifest-schema" in capsys.readouterr().err


def test_merging_keeps_their_manifest_and_appends_missing_teams(tmp_path):
    original = LAB_MANIFEST.read_text(encoding="utf-8")
    chosen = options(tmp_path, manifest=LAB_MANIFEST)

    init(chosen, fake_gateway(), INIT_TOKEN)

    merged = chosen.manifest_out.read_text(encoding="utf-8")
    assert merged.startswith(original)
    assert Manifest.from_file(chosen.manifest_out).team_policy("search") is not None


def test_an_unreadable_gateway_exits_two(tmp_path, capsys):
    assert init(options(tmp_path), fake_gateway(status=401), INIT_TOKEN) == 2
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
        INIT_TOKEN,
    )

    assert exit_code == 1
    assert not chosen.manifest_out.exists()
    assert "team-in-manifest\tmanifest\t" in capsys.readouterr().out


@pytest.mark.parametrize("write_files", [False, True])
def test_init_prints_no_value_from_the_gateway_config(tmp_path, capsys, write_files):
    chosen = options(
        tmp_path,
        gateway_config=config_with_inline_secrets(tmp_path),
        write=write_files,
    )

    init(chosen, fake_gateway(), INIT_TOKEN)

    printed = capsys.readouterr()
    assert SECRET not in printed.out + printed.err
    assert "  add brownout.gateway.handler to litellm_settings.callbacks" in printed.out
    assert (
        "  set general_settings.coordination_redis with host, port, password"
        in printed.out
    )


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        (
            f"general_settings:\n  master_key: sk-{SECRET}\n  master_key: sk-{SECRET}\n",
            "has a duplicate key at line 3, column 3",
        ),
        (
            f'general_settings:\n  master_key: "sk-{SECRET}\n',
            "is not valid YAML at line 3",
        ),
    ],
)
def test_an_unreadable_config_is_reported_by_position_only(
    tmp_path, capsys, text, problem
):
    config = tmp_path / "config.yaml"
    config.write_text(text, encoding="utf-8")

    exit_code = init(
        options(tmp_path, gateway_config=config),
        fake_gateway(),
        INIT_TOKEN,
    )

    error = capsys.readouterr().err
    assert exit_code == 2
    assert f"{config} {problem}" in error
    assert SECRET not in error


def test_the_config_keeps_its_permissions(tmp_path):
    config = lab_config_copy(tmp_path)
    config.chmod(0o600)
    chosen = options(tmp_path, gateway_config=config)

    init(chosen, fake_gateway(), INIT_TOKEN)

    assert chosen.config_out.stat().st_mode & 0o777 == 0o600


def test_init_without_its_key_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BROWNOUT_INIT_KEY", raising=False)

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
    assert "BROWNOUT_INIT_KEY" in capsys.readouterr().err


def test_the_key_init_runs_with_does_not_block_readiness(tmp_path):
    own_key = {**TEAM_KEY, "token": INIT_TOKEN, "key_alias": None, "team_id": None}

    exit_code = init(
        options(tmp_path),
        fake_gateway(keys=[TEAM_KEY, own_key]),
        INIT_TOKEN,
    )

    assert exit_code == 0


def test_a_team_key_running_init_is_left_out_of_the_admin_keys_notice(tmp_path, capsys):
    own_key = {**TEAM_KEY, "token": INIT_TOKEN}

    init(options(tmp_path), fake_gateway(keys=[own_key]), INIT_TOKEN)

    notice = next(
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("team-in-manifest\tmaster key\t")
    )
    assert "init runs with" not in notice


def test_init_only_reads_the_gateway(tmp_path):
    methods: list[str] = []

    init(options(tmp_path), fake_gateway(methods=methods), INIT_TOKEN)

    assert methods
    assert set(methods) == {"GET"}


def test_check_only_reads_the_gateway():
    methods: list[str] = []

    read_model_groups(fake_gateway(methods=methods))

    assert set(methods) == {"GET"}


def test_the_plugin_command_writes_the_plugin_folder(tmp_path, capsys):
    plugins = tmp_path / "plugins"

    exit_code = main(["plugin", "--dir", str(plugins)])

    assert exit_code == 0
    assert (plugins / "brownout" / "gateway.py").exists()
    assert f"wrote {plugins / 'brownout'}" in capsys.readouterr().out


def check_files(config: Path, manifest: Path) -> int:
    return main(["check", "--gateway-config", str(config), "--manifest", str(manifest)])


def test_a_fallback_without_data_classes_passes_when_the_manifest_lists_none(tmp_path):
    config = yaml.safe_load(LAB_CONFIG.read_text(encoding="utf-8"))
    for deployment in config["model_list"]:
        deployment["model_info"].pop("data_class", None)
    manifest = yaml.safe_load(LAB_MANIFEST.read_text(encoding="utf-8"))
    del manifest["data_class_order"]

    assert (
        check_files(
            write(tmp_path / "config.yaml", config),
            write(tmp_path / "manifest.yaml", manifest),
        )
        == 0
    )


def test_a_fallback_without_data_classes_fails_when_the_manifest_lists_them(
    tmp_path, capsys
):
    config = yaml.safe_load(LAB_CONFIG.read_text(encoding="utf-8"))
    for deployment in config["model_list"]:
        deployment["model_info"].pop("data_class", None)

    exit_code = check_files(write(tmp_path / "config.yaml", config), LAB_MANIFEST)

    assert exit_code == 1
    assert "data-class-declared\t" in capsys.readouterr().out


def test_check_blames_a_malformed_manifest_on_the_manifest(tmp_path, capsys):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("version: [\n", encoding="utf-8")

    exit_code = check_files(LAB_CONFIG, manifest)

    printed = capsys.readouterr()
    assert exit_code == 2
    assert printed.err == f"{manifest} is not valid YAML at line 2, column 1\n"
    assert printed.out == ""


def test_check_refuses_a_missing_manifest(tmp_path, capsys):
    manifest = tmp_path / "missing.yaml"

    exit_code = check_files(LAB_CONFIG, manifest)

    assert exit_code == 2
    assert capsys.readouterr().err == f"{manifest} was not found\n"


@pytest.mark.parametrize(
    ("name", "content", "problem"),
    [
        pytest.param("missing.yaml", None, "was not found", id="missing"),
        pytest.param(
            "binary.yaml", b"\xff\n", "is not UTF-8 text at byte 0", id="not-utf8"
        ),
        pytest.param("empty.yaml", b"", "is not a YAML mapping", id="empty"),
        pytest.param("list.yaml", b"- a\n- b\n", "is not a YAML mapping", id="list"),
    ],
)
def test_check_refuses_a_gateway_config_it_cannot_use(
    tmp_path, capsys, name, content, problem
):
    config = tmp_path / name
    if content is not None:
        config.write_bytes(content)

    exit_code = check_files(config, LAB_MANIFEST)

    printed = capsys.readouterr()
    assert exit_code == 2
    assert printed.err == f"{config} {problem}\n"
    assert printed.out == ""


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        pytest.param(b"\xff\n", "is not UTF-8 text at byte 0", id="not-utf8"),
        pytest.param(b"", "is not a YAML mapping", id="empty"),
        pytest.param(b"- a\n", "is not a YAML mapping", id="list"),
    ],
)
def test_init_refuses_a_gateway_config_it_cannot_use(
    tmp_path, capsys, content, problem
):
    config = tmp_path / "config.yaml"
    config.write_bytes(content)

    exit_code = init(
        options(tmp_path, gateway_config=config),
        fake_gateway(),
        INIT_TOKEN,
    )

    assert exit_code == 2
    assert f"{config} {problem}" in capsys.readouterr().err


def test_init_reports_a_malformed_manifest_by_position(tmp_path, capsys):
    manifest = tmp_path / "theirs.yaml"
    manifest.write_text("version: [\n", encoding="utf-8")

    exit_code = init(
        options(tmp_path, manifest=manifest),
        fake_gateway(),
        INIT_TOKEN,
    )

    assert exit_code == 2
    assert f"{manifest} is not valid YAML at line 2, column 1" in (
        capsys.readouterr().err
    )


@pytest.mark.parametrize(
    ("written", "priced"),
    [
        pytest.param("1e-4", True, id="scientific-without-a-point"),
        pytest.param("0.0001", True, id="decimal-string"),
    ],
)
def test_a_string_price_is_read_the_way_litellm_reads_it(written, priced):
    gateway = gateway_file_of(
        {
            "model_list": [
                {
                    "model_name": "chat",
                    "litellm_params": {
                        "model": "openai/never-mapped",
                        "input_cost_per_token": written,
                        "output_cost_per_token": written,
                    },
                }
            ]
        }
    )

    assert (gateway.groups["chat"].input_cost_per_token is not None) is priced


def split_lab_config(directory: Path) -> Path:
    config = lab_gateway_config()
    models = {"model_list": config.pop("model_list")}
    config["include"] = ["models.yaml"]
    write(directory / "models.yaml", models)
    return write(directory / "config.yaml", config)


def test_check_reads_models_from_included_files(tmp_path):
    exit_code = check_files(split_lab_config(tmp_path), LAB_MANIFEST)

    assert exit_code == 0


def test_init_reads_models_from_included_files(tmp_path):
    chosen = options(
        tmp_path,
        gateway_config=split_lab_config(tmp_path),
        manifest=LAB_MANIFEST,
        manifest_out=tmp_path / "out.yaml",
    )

    init(chosen, fake_gateway(), INIT_TOKEN)

    assert check_files(chosen.config_out, chosen.manifest_out) == 0


def test_check_sees_gateway_models_a_file_does_not_list(tmp_path):
    config = lab_gateway_config()
    database_models = {
        name: group
        for name, group in gateway_file_of(config).groups.items()
        if name.startswith("budget-lab")
    }
    config["model_list"] = [
        deployment
        for deployment in config["model_list"]
        if not deployment["model_name"].startswith("budget-lab")
    ]
    config_path = write(tmp_path / "config.yaml", config)

    assert check(config_path, LAB_MANIFEST, {}) != []
    assert check(config_path, LAB_MANIFEST, database_models) == []


def test_check_with_a_gateway_needs_the_init_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BROWNOUT_INIT_KEY", raising=False)

    exit_code = main(
        [
            "check",
            "--gateway",
            "http://gateway.test",
            "--gateway-config",
            str(LAB_CONFIG),
            "--manifest",
            str(LAB_MANIFEST),
        ]
    )

    assert exit_code == 2
    assert "BROWNOUT_INIT_KEY" in capsys.readouterr().err


@pytest.mark.parametrize("written", ["not a price", "nan"])
def test_an_explicit_price_litellm_cannot_read_is_refused(written):
    with pytest.raises(DocumentError, match="must be a finite number"):
        gateway_file_of(
            {
                "model_list": [
                    {
                        "model_name": "chat",
                        "litellm_params": {
                            "model": "openai/never-mapped",
                            "input_cost_per_token": written,
                            "output_cost_per_token": written,
                        },
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    ("model", "priced"),
    [
        pytest.param("gpt-4o", True, id="cost-map-key"),
        pytest.param("openai/gpt-4o", True, id="bare-name-is-a-key"),
        pytest.param("openai/never-mapped", False, id="not-in-the-cost-map"),
    ],
)
def test_the_cost_map_prices_a_model_by_its_full_or_bare_name(model, priced):
    gateway = gateway_file_of(
        {"model_list": [{"model_name": "chat", "litellm_params": {"model": model}}]}
    )

    assert (gateway.groups["chat"].input_cost_per_token is not None) is priced


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["check", "--manifest", "m.yaml"], id="check-without-config"),
        pytest.param(["init", "--gateway-config", "c.yaml"], id="init-without-gateway"),
        pytest.param([], id="no-command"),
    ],
)
def test_a_command_without_its_required_flags_exits_two(arguments):
    with pytest.raises(SystemExit) as exited:
        main(arguments)

    assert exited.value.code == 2
