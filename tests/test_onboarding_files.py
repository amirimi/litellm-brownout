from __future__ import annotations

from typing import Final

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

        assert dump_document(load_document(text)) == text


def test_config_changes_keep_comments_and_order():
    document = load_document(COMMENTED_CONFIG)

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
    assert to_plain(load_document(text)) == {
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
    document = load_document("model_list: []\n")

    apply_config_changes(
        document,
        (AppendSetting("litellm_settings", "callbacks", "brownout.gateway.handler"),),
    )

    assert to_plain(document)["litellm_settings"] == {
        "callbacks": ["brownout.gateway.handler"]
    }


def test_appended_teams_leave_existing_entries_untouched():
    original = (CONFIG_DIR / "manifest.yaml").read_text(encoding="utf-8")
    document = load_document(original)

    append_teams(document, (TeamEntry("search", 500.0, "7d"),))
    text = dump_document(document)

    assert text.startswith(original)
    assert to_plain(load_document(text))["teams"]["search"]["policy_epoch"] == (
        "search-v1"
    )


def test_a_new_manifest_holds_only_the_given_teams():
    document = new_manifest_document((TeamEntry("search", 500.0, "7d"),))

    assert to_plain(load_document(dump_document(document))) == {
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
