from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
import yaml

from brownout.onboarding import AppendSetting, RemoveSetting, SetSetting, TeamEntry
from brownout.onboarding_files import (
    append_teams,
    apply_config_changes,
    dump_document,
    load_document,
    new_manifest_document,
    to_plain,
    unified_diff,
)

from .support import CONFIG_DIR

SOURCE: Final = Path("config.yaml")
COMMENTED_CONFIG: Final = """# Gateway for the search org
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY  # rotated monthly
  disable_budget_reservation: true

litellm_settings:
  callbacks: ["langfuse"]

model_list:
  - model_name: chat
    litellm_params:
      model: openai/gpt-4o-mini
"""


def test_the_lab_files_round_trip_byte_for_byte():
    for name in ("config.yaml", "manifest.yaml"):
        text = (CONFIG_DIR / name).read_text(encoding="utf-8")

        assert dump_document(load_document(SOURCE, text)) == text


def test_config_changes_keep_comments_and_order():
    document = load_document(SOURCE, COMMENTED_CONFIG)

    apply_config_changes(
        document,
        (
            AppendSetting("litellm_settings", "callbacks", "brownout.gateway.handler"),
            SetSetting(
                "general_settings", "fail_closed_budget_enforcement", True, None
            ),
            RemoveSetting("general_settings", "disable_budget_reservation"),
        ),
    )
    text = dump_document(document)

    assert "# Gateway for the search org" in text
    assert "# rotated monthly" in text
    assert "disable_budget_reservation" not in text
    assert to_plain(load_document(SOURCE, text)) == {
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "fail_closed_budget_enforcement": True,
        },
        "litellm_settings": {"callbacks": ["langfuse", "brownout.gateway.handler"]},
        "model_list": [
            {"model_name": "chat", "litellm_params": {"model": "openai/gpt-4o-mini"}}
        ],
    }


def test_a_missing_section_is_created():
    document = load_document(SOURCE, "model_list: []\n")

    apply_config_changes(
        document,
        (AppendSetting("litellm_settings", "callbacks", "brownout.gateway.handler"),),
    )

    assert to_plain(document)["litellm_settings"] == {
        "callbacks": ["brownout.gateway.handler"]
    }


def test_appended_teams_leave_existing_entries_untouched():
    original = (CONFIG_DIR / "manifest.yaml").read_text(encoding="utf-8")
    document = load_document(SOURCE, original)

    append_teams(document, (TeamEntry("search", 500.0, "7d"),))
    text = dump_document(document)

    assert text.startswith(original)
    assert to_plain(load_document(SOURCE, text))["teams"]["search"]["policy_epoch"] == (
        "search-v1"
    )


def test_a_new_manifest_holds_only_the_given_teams():
    document = new_manifest_document((TeamEntry("search", 500.0, "7d"),))

    assert to_plain(load_document(SOURCE, dump_document(document))) == {
        "version": "v1",
        "teams": {
            "search": {
                "policy_epoch": "search-v1",
                "envelope_usd": 500.0,
                "budget_duration": "7d",
                "warn_tiers": [75, 90],
                "routes": {},
            }
        },
    }


def test_the_diff_names_the_file_and_shows_only_changes():
    diff = unified_diff("a: 1\nb: 2\n", "a: 1\nb: 3\n", "config.yaml")

    assert diff.splitlines()[:2] == ["--- config.yaml", "+++ config.yaml"]
    assert "-b: 2" in diff and "+b: 3" in diff
    assert unified_diff("same\n", "same\n", "x") == ""


FLUSH_LISTS: Final = """model_list:
- model_name: chat
  litellm_params:
    model: openai/mock-chat
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"""
INDENTED_LISTS: Final = """model_list:
  - model_name: chat
    litellm_params:
      model: openai/mock-chat
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"""
FOUR_SPACE_MAPS: Final = """general_settings:
    master_key: os.environ/LITELLM_MASTER_KEY
model_list:
    - model_name: chat
      litellm_params:
          model: openai/mock-chat
"""
NO_LISTS: Final = """general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"""


@pytest.mark.parametrize(
    "text",
    [FLUSH_LISTS, INDENTED_LISTS, FOUR_SPACE_MAPS, NO_LISTS],
    ids=["flush", "indented", "four-space", "no-lists"],
)
def test_every_list_style_round_trips_byte_for_byte(text):
    assert dump_document(load_document(SOURCE, text)) == text


@pytest.mark.parametrize(
    "text",
    [FLUSH_LISTS, INDENTED_LISTS, NO_LISTS],
    ids=["flush", "indented", "no-lists"],
)
def test_a_prepared_config_stays_valid_yaml_in_every_list_style(text):
    document = load_document(SOURCE, text)

    apply_config_changes(
        document,
        [
            AppendSetting("litellm_settings", "callbacks", "brownout.gateway.handler"),
            SetSetting(
                "general_settings", "coordination_redis", {"host": "redis"}, None
            ),
        ],
    )

    reloaded = yaml.safe_load(dump_document(document))
    assert reloaded["litellm_settings"]["callbacks"] == ["brownout.gateway.handler"]
    assert reloaded["general_settings"]["coordination_redis"] == {"host": "redis"}
