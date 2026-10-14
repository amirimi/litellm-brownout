from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
import yaml

from .adoption.findings import PrintedFinding, parse_findings
from .adoption.readme import README, adapt, readme_blocks
from .adoption.resolutions import (
    MissingChoice,
    fill,
    kind_of,
    merge_config,
    primary_resolution,
)
from .adoption.scenario import load_scenario, scenario_names
from .adoption.shell import ShellSession
from .adoption.yamlfile import parse_snippet, read_styled
from .support import REPO_ROOT

INIT_OUTPUT: Final = """brownout.config.yaml changes config.yaml as follows:
  add brownout.gateway.handler to litellm_settings.callbacks
note: settings saved through LiteLLM's admin UI live in its database and are not part of these changes
--- brownout.yaml
+++ brownout.yaml
@@ -0,0 +1,2 @@
+version: v1
+teams: {}
team-in-manifest\tdev-laptop\thas no team, so its chat calls will be refused
    curl -X POST http://localhost:4000/key/update -H "Authorization: Bearer $LITELLM_MASTER_KEY" -d '{"key": "abc", "team_id": "<YOUR TEAM ID>"}'
team-in-manifest\tadmin keys\tthe master key has no team
model-priced\tchat\thas no input and output price, so requests for it will be refused
    - model_name: chat
      litellm_params:
        input_cost_per_token: <YOUR INPUT PRICE PER TOKEN>
        output_cost_per_token: <YOUR OUTPUT PRICE PER TOKEN>
model-priced\t...\tand 3 more
dry run: nothing written. Rerun with --write to write both files
"""


def test_every_marked_readme_block_is_found():
    blocks = readme_blocks(README.read_text(encoding="utf-8"))

    assert set(blocks) == {
        "prepare",
        "check",
        "install",
        "confirm",
        "fallback-manifest",
        "fallback-config",
    }
    assert "brownout init" in blocks["prepare"]
    assert blocks["install"].startswith("FROM ")


def test_a_marker_without_a_fenced_block_is_refused():
    with pytest.raises(ValueError, match="adoption: prepare"):
        readme_blocks("<!-- adoption: prepare -->\nprose instead of a block\n")


def test_adapting_a_block_needs_every_original_text():
    assert adapt(
        "curl http://localhost:4000/x", [("localhost:4000", "localhost:9")]
    ) == ("curl http://localhost:9/x")
    with pytest.raises(ValueError, match="localhost:4001"):
        adapt("curl http://localhost:4000/x", [("localhost:4001", "localhost:9")])


def test_init_output_parses_into_findings_with_their_resolutions():
    findings = parse_findings(INIT_OUTPUT)

    assert [finding.key for finding in findings] == [
        ("team-in-manifest", "dev-laptop"),
        ("team-in-manifest", "admin keys"),
        ("model-priced", "chat"),
        ("model-priced", "..."),
    ]
    assert findings[0].resolution[0].startswith("curl -X POST")
    assert findings[1].resolution == ()
    assert findings[2].resolution[0] == "- model_name: chat"
    assert findings[3].truncated


def test_resolution_kinds_follow_the_first_printed_line():
    findings = parse_findings(INIT_OUTPUT)

    assert [kind_of(finding) for finding in findings[:3]] == [
        "command",
        "none",
        "config",
    ]


def test_placeholders_are_filled_only_from_declared_values():
    filled = fill(
        '{"team_id": "<YOUR TEAM ID>"}', {"<YOUR TEAM ID>": "88fa", "<A MODEL>": "x"}
    )

    assert filled == '{"team_id": "88fa"}'
    with pytest.raises(MissingChoice, match="<YOUR BUDGET>"):
        fill('{"max_budget": <YOUR BUDGET>}', {})


def test_a_placeholder_with_quotes_and_commas_is_one_placeholder():
    placeholder = '<YOUR DURATION IN DOUBLE QUOTES, SUCH AS "7d">'

    assert fill(f'{{"budget_duration": {placeholder}}}', {placeholder: '"7d"'}) == (
        '{"budget_duration": "7d"}'
    )


def test_a_model_snippet_merges_into_every_deployment_of_its_group():
    config = {
        "model_list": [
            {"model_name": "chat", "litellm_params": {"model": "openai/a"}},
            {"model_name": "chat", "litellm_params": {"model": "openai/b"}},
            {"model_name": "other", "litellm_params": {"model": "openai/c"}},
        ]
    }
    snippet = [
        {"model_name": "chat", "litellm_params": {"input_cost_per_token": 0.1}},
    ]

    assert merge_config(config, snippet) == []
    assert [
        d["litellm_params"].get("input_cost_per_token") for d in config["model_list"]
    ] == [
        0.1,
        0.1,
        None,
    ]
    assert config["model_list"][0]["litellm_params"]["model"] == "openai/a"


def test_a_model_snippet_for_a_group_the_file_lacks_is_reported():
    config = {"model_list": [{"model_name": "chat", "litellm_params": {}}]}

    assert merge_config(config, [{"model_name": "ui-model", "litellm_params": {}}]) == [
        "ui-model"
    ]


def test_a_settings_snippet_merges_into_its_section():
    config = {"general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"}}

    merge_config(
        config, {"general_settings": {"coordination_redis": {"host": "redis"}}}
    )

    assert config["general_settings"] == {
        "master_key": "os.environ/LITELLM_MASTER_KEY",
        "coordination_redis": {"host": "redis"},
    }


def test_the_shell_keeps_variables_and_their_export_flags_between_steps(
    tmp_path: Path,
):
    shell = ShellSession(tmp_path, {}, ("SECRET", "VISIBLE"))

    shell.run(
        "read -rs SECRET && typeset +x SECRET\nexport VISIBLE=shown", stdin="hidden\n"
    )
    later = shell.run(
        'echo "$SECRET"; sh -c \'echo "${SECRET:-unexported} ${VISIBLE}"\''
    )

    assert later.stdout.splitlines() == ["hidden", "unexported shown"]


def test_the_shell_starts_without_the_test_process_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-from-the-test-process")

    result = ShellSession(tmp_path, {}, ()).run('echo "${LITELLM_MASTER_KEY:-absent}"')

    assert result.stdout.strip() == "absent"


def test_a_printed_finding_knows_whether_it_was_cut_short():
    assert PrintedFinding("model-priced", "...", "and 3 more", ()).truncated
    assert not PrintedFinding("model-priced", "chat", "has no price", ()).truncated


FLUSH_CONFIG: Final = """model_list:
- model_name: chat
  litellm_params:
    model: openai/mock-chat
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"""
INDENTED_CONFIG: Final = """model_list:
  - model_name: chat
    litellm_params:
      model: openai/mock-chat
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"""


@pytest.mark.parametrize(
    "text", [FLUSH_CONFIG, INDENTED_CONFIG], ids=["flush", "indented"]
)
def test_a_styled_file_round_trips_unchanged(tmp_path: Path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")

    read_styled(path).save()

    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    "text", [FLUSH_CONFIG, INDENTED_CONFIG], ids=["flush", "indented"]
)
def test_an_edit_keeps_the_file_in_its_own_list_style(tmp_path: Path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    document = read_styled(path)

    merge_config(
        document.data,
        [{"model_name": "chat", "litellm_params": {"input_cost_per_token": 0.1}}],
    )
    document.save()

    first_item = next(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if "- model_name" in line
    )
    expected = next(line for line in text.splitlines() if "- model_name" in line)
    assert first_item == expected
    assert "input_cost_per_token: 0.1" in path.read_text(encoding="utf-8")


def test_a_printed_value_keeps_its_literal_form(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(INDENTED_CONFIG, encoding="utf-8")
    document = read_styled(path)

    merge_config(
        document.data,
        parse_snippet(
            "- model_name: chat\n  litellm_params:\n    input_cost_per_token: 0.00005\n"
        ),
    )
    document.save()

    assert "input_cost_per_token: 0.00005" in path.read_text(encoding="utf-8")


def test_a_command_resolution_runs_without_its_printed_alternative():
    finding = PrintedFinding(
        "ceiling-matches-envelope",
        "team-a",
        "envelope_usd is 2.0 and LiteLLM's max_budget is 1.0",
        (
            'curl -X POST http://gw/team/update -d \'{"team_id": "team-a"}\'',
            "or set teams.team-a.envelope_usd: 1.0",
        ),
    )

    assert primary_resolution(finding) == (finding.resolution[0],)


def test_the_ci_matrix_runs_every_scenario():
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "adoption.yml").read_text(
            encoding="utf-8"
        )
    )

    matrix = workflow["jobs"]["scenario"]["strategy"]["matrix"]

    assert matrix["scenario"] == scenario_names()


def test_every_scenario_loads():
    for name in scenario_names():
        assert load_scenario(name).description
