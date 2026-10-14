from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import pytest

from brownout.gateway_api import GatewaySnapshot, LiteLLMTeam, read_gateway
from brownout.manifest import Manifest
from brownout.onboarding import (
    BUDGET_PLACEHOLDER,
    DURATION_PLACEHOLDER,
    TEAM_PLACEHOLDER,
    Finding,
    nothing_to_govern,
    reconcile_findings,
    team_entries,
    team_findings,
)
from brownout.refusals import RuleId

from .lab import (
    GATEWAY,
    REQUEST_TIMEOUT_SECONDS,
    admin_headers,
    requires_stack,
    run_printed,
)

pytestmark = [pytest.mark.e2e, requires_stack]

FILLED_BUDGET: Final = "1.0"
FILLED_DURATION: Final = '"7d"'
SUCCESS: Final = 200
LAB_ROUTE: Final = "budget-lab-primary"
LAB_FALLBACK: Final = "budget-lab-degraded"


@pytest.fixture
def suffix() -> str:
    return datetime.now(UTC).strftime("%H%M%S%f")


@pytest.fixture
def teams() -> Iterator[list[str]]:
    created: list[str] = []
    yield created
    if created:
        httpx.post(
            f"{GATEWAY}/team/delete",
            headers=admin_headers(),
            json={"team_ids": created},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )


@pytest.fixture
def users() -> Iterator[list[str]]:
    created: list[str] = []
    yield created
    if created:
        httpx.post(
            f"{GATEWAY}/user/delete",
            headers=admin_headers(),
            json={"user_ids": created},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )


def _post(path: str, body: dict[str, Any]) -> None:
    response: Final = httpx.post(
        f"{GATEWAY}{path}",
        headers=admin_headers(),
        json=body,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def _snapshot() -> GatewaySnapshot:
    with httpx.Client(
        base_url=GATEWAY, headers=admin_headers(), timeout=REQUEST_TIMEOUT_SECONDS
    ) as client:
        return read_gateway(client, lambda page, total: None)


def _team(team_id: str) -> LiteLLMTeam:
    found: Final = [team for team in _snapshot().teams if team.team_id == team_id]
    assert len(found) == 1, f"/team/list does not list {team_id} once"
    return found[0]


def _filled(command: str, team_id: str) -> str:
    return (
        command.replace(TEAM_PLACEHOLDER, team_id)
        .replace(BUDGET_PLACEHOLDER, FILLED_BUDGET)
        .replace(DURATION_PLACEHOLDER, FILLED_DURATION)
    )


def _member_findings(team: LiteLLMTeam) -> list[Finding]:
    return [
        finding
        for finding in team_findings([team], GATEWAY)
        if finding.rule is RuleId.MEMBER_HAS_NO_BUDGET
    ]


def _manifest(team_id: str, team: dict[str, Any]) -> Manifest:
    return Manifest.from_document(
        "contract.yaml",
        {
            "version": "v1",
            "data_class_order": ["internal", "customer_pii"],
            "teams": {
                team_id: {
                    "policy_epoch": f"{team_id}-v1",
                    "envelope_usd": 1.0,
                    "budget_duration": "7d",
                    **team,
                }
            },
        },
    )


def test_the_member_command_clears_a_budget_set_on_one_member(teams, users, suffix):
    team_id = f"contract-member-{suffix}"
    user_id = f"contract-user-{suffix}"
    _post("/team/new", {"team_id": team_id, "max_budget": 1.0, "budget_duration": "7d"})
    teams.append(team_id)
    _post("/user/new", {"user_id": user_id})
    users.append(user_id)
    _post(
        "/team/member_add",
        {
            "team_id": team_id,
            "member": {"user_id": user_id, "role": "user"},
            "max_budget_in_team": 0.5,
        },
    )
    assert user_id in _team(team_id).budgeted_members

    for finding in _member_findings(_team(team_id)):
        assert run_printed(finding.resolution[0]) == SUCCESS, finding.resolution[0]

    assert _team(team_id).budgeted_members == ()


def test_the_member_command_clears_a_team_wide_budget_on_existing_members(
    teams, users, suffix
):
    team_id = f"contract-wide-{suffix}"
    user_id = f"contract-wide-user-{suffix}"
    _post(
        "/team/new",
        {
            "team_id": team_id,
            "max_budget": 1.0,
            "budget_duration": "7d",
            "team_member_budget": 0.5,
        },
    )
    teams.append(team_id)
    _post("/user/new", {"user_id": user_id})
    users.append(user_id)
    _post(
        "/team/member_add",
        {"team_id": team_id, "member": {"user_id": user_id, "role": "user"}},
    )
    assert user_id in _team(team_id).budgeted_members

    for finding in _member_findings(_team(team_id)):
        assert run_printed(finding.resolution[0]) == SUCCESS, finding.resolution[0]

    assert _team(team_id).budgeted_members == ()


def test_the_team_command_gives_a_team_its_budget_and_window(teams, suffix):
    team_id = f"contract-unbudgeted-{suffix}"
    _post("/team/new", {"team_id": team_id})
    teams.append(team_id)
    findings = team_findings([_team(team_id)], GATEWAY)
    assert [finding.rule for finding in findings] == [RuleId.TEAM_IN_MANIFEST]

    assert run_printed(_filled(findings[0].resolution[0], team_id)) == SUCCESS

    entries = team_entries([_team(team_id)], frozenset())
    assert [
        (entry.team_id, entry.envelope_usd, entry.budget_duration) for entry in entries
    ] == [(team_id, 1.0, "7d")]


def test_the_first_team_commands_create_a_governable_team_and_its_key(teams, suffix):
    team_id = f"contract-first-{suffix}"
    commands = nothing_to_govern(GATEWAY, False).resolution
    teams.append(team_id)

    assert [run_printed(_filled(command, team_id)) for command in commands] == [
        SUCCESS,
        SUCCESS,
    ]

    snapshot = _snapshot()
    assert [entry.team_id for entry in team_entries(snapshot.teams, frozenset())].count(
        team_id
    ) == 1
    assert any(key.team_id == team_id for key in snapshot.keys)


def test_the_missing_team_command_creates_the_manifest_team(teams, suffix):
    team_id = f"contract-missing-{suffix}"
    manifest = _manifest(team_id, {})
    findings = reconcile_findings(manifest, _snapshot().teams, GATEWAY)
    assert [finding.rule for finding in findings] == [RuleId.TEAM_EXISTS]
    teams.append(team_id)

    assert run_printed(findings[0].resolution[0]) == SUCCESS

    assert reconcile_findings(manifest, _snapshot().teams, GATEWAY) == []


def test_the_alignment_commands_match_litellm_to_the_manifest(teams, suffix):
    team_id = f"contract-drifted-{suffix}"
    _post(
        "/team/new",
        {
            "team_id": team_id,
            "max_budget": 2.0,
            "budget_duration": "1d",
            "models": [LAB_ROUTE],
        },
    )
    teams.append(team_id)
    manifest = _manifest(
        team_id, {"routes": {LAB_ROUTE: {"fallback": LAB_FALLBACK, "degrade_at": 90}}}
    )
    findings = reconcile_findings(manifest, _snapshot().teams, GATEWAY)
    assert {finding.rule for finding in findings} == {
        RuleId.CEILING_MATCHES_ENVELOPE,
        RuleId.DURATION_MATCHES,
        RuleId.FALLBACK_IN_TEAM_MODELS,
    }

    assert [run_printed(finding.resolution[0]) for finding in findings] == [SUCCESS] * 3

    assert reconcile_findings(manifest, _snapshot().teams, GATEWAY) == []
