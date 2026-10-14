from __future__ import annotations

from typing import Final

from .model import Form, Input, Supported

CONTRACT: Final = "tests/test_printed_commands_e2e.py::"
SMOKE: Final = "tests/test_smoke_e2e.py::"
ADOPTION: Final = (
    "tests/test_adoption.py::test_the_printed_path_meets_only_the_declared_gaps"
)
TEAMS: Final = "litellm/proxy/management_endpoints/team_endpoints.py"
KEYS: Final = "litellm/proxy/management_endpoints/key_management_endpoints.py"
MODELS: Final = "litellm/proxy/management_endpoints/model_management_endpoints.py"
USERS: Final = "litellm/proxy/management_endpoints/internal_user_endpoints.py"
EVERY_RELEASE_SUCCEEDS: Final = "every supported release"


def _succeeds(*tests: str) -> tuple[Form, ...]:
    return (Form(EVERY_RELEASE_SUCCEEDS, "the finding resolves", Supported(tests)),)


PRINTED_ROWS: Final = (
    Input(
        shape="printed /team/member_update clearing a member budget",
        source="printed",
        parser="onboarding._member_finding",
        reads=(),
        cites=(f"{TEAMS}:3431@1.101.6",),
        forms=_succeeds(
            f"{CONTRACT}test_the_member_command_clears_a_budget_set_on_one_member",
            f"{CONTRACT}test_the_member_command_clears_a_team_wide_budget_on_existing_members",
        ),
    ),
    Input(
        shape="printed /team/update giving a team a budget and window",
        source="printed",
        parser="onboarding._team_finding",
        reads=(),
        cites=(f"{TEAMS}:1905@1.101.6",),
        forms=_succeeds(
            f"{CONTRACT}test_the_team_command_gives_a_team_its_budget_and_window"
        ),
    ),
    Input(
        shape="printed /team/new and /key/generate for a gateway without teams",
        source="printed",
        parser="onboarding.nothing_to_govern",
        reads=(),
        cites=(f"{TEAMS}:1183@1.101.6", f"{KEYS}:1697@1.101.6"),
        forms=_succeeds(
            f"{CONTRACT}test_the_first_team_commands_create_a_governable_team_and_its_key"
        ),
    ),
    Input(
        shape="printed /team/new for a manifest team LiteLLM lacks",
        source="printed",
        parser="onboarding._missing_team",
        reads=(),
        cites=(f"{TEAMS}:1183@1.101.6",),
        forms=_succeeds(
            f"{CONTRACT}test_the_missing_team_command_creates_the_manifest_team"
        ),
    ),
    Input(
        shape="printed /team/update aligning max_budget with the envelope",
        source="printed",
        parser="onboarding._envelope_mismatch",
        reads=(),
        cites=(f"{TEAMS}:1905@1.101.6",),
        forms=_succeeds(
            f"{CONTRACT}test_the_alignment_commands_match_litellm_to_the_manifest"
        ),
    ),
    Input(
        shape="printed /team/update aligning budget_duration",
        source="printed",
        parser="onboarding._duration_mismatch",
        reads=(),
        cites=(f"{TEAMS}:1905@1.101.6",),
        forms=_succeeds(
            f"{CONTRACT}test_the_alignment_commands_match_litellm_to_the_manifest"
        ),
    ),
    Input(
        shape="printed /team/update adding a fallback to the team's models",
        source="printed",
        parser="onboarding._unlisted_fallbacks",
        reads=(),
        cites=(f"{TEAMS}:1905@1.101.6",),
        forms=_succeeds(
            f"{CONTRACT}test_the_alignment_commands_match_litellm_to_the_manifest"
        ),
    ),
    Input(
        shape="printed /key/update for a teamless key or a key's own budget",
        source="printed",
        parser="onboarding.key_findings",
        reads=(),
        cites=(f"{KEYS}:2829@1.101.6",),
        forms=_succeeds(
            f"{SMOKE}test_init_from_nothing_prepares_a_gateway_and_its_commands_resolve_the_findings"
        ),
    ),
    Input(
        shape="printed price lines for an unpriced model",
        source="printed",
        parser="onboarding.model_findings",
        reads=(),
        cites=("litellm/router.py:322@1.101.6",),
        forms=_succeeds(ADOPTION),
    ),
    Input(
        shape="printed coordination_redis block",
        source="printed",
        parser="onboarding._violation_resolution",
        reads=(),
        cites=("litellm/proxy/proxy_server.py:4243@1.101.6",),
        forms=_succeeds(ADOPTION),
    ),
    Input(
        shape="README commands that call LiteLLM",
        source="printed",
        parser=None,
        reads=(),
        cites=(f"{USERS}:437@1.101.6", f"{MODELS}:791@1.101.6"),
        forms=_succeeds(ADOPTION),
    ),
)
