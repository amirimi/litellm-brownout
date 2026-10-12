from __future__ import annotations

from brownout import bridge

from .support import RecordingSink, build_handler, disclosed, make_auth, request


def broken_router() -> None:
    raise RuntimeError("router not initialised")


async def degrade_with(team, team_models, key_models=None, permissions=None):
    sink = RecordingSink()
    handler = build_handler(
        team, sink, spend=team.envelope_usd * 0.95, permissions=permissions
    )
    auth = make_auth(team)
    auth.team_models = team_models
    if key_models is not None:
        auth.models = key_models
    return await request(handler, auth, "budget-lab-primary"), sink


async def test_a_target_outside_the_team_allowlist_is_not_served(team):
    applied, _ = await degrade_with(team, ["budget-lab-primary", "budget-lab-strict"])

    assert applied["model"] == "budget-lab-primary"
    assert disclosed(applied)["degradation_reason"] == "fallback_rejected"


async def test_a_target_outside_the_key_allowlist_is_not_served(team):
    applied, _ = await degrade_with(
        team,
        ["budget-lab-primary", "budget-lab-degraded"],
        key_models=["budget-lab-primary"],
    )

    assert applied["model"] == "budget-lab-primary"
    assert disclosed(applied)["degradation_reason"] == "fallback_rejected"


async def test_a_key_inheriting_its_team_allowlist_may_be_degraded(team):
    applied, _ = await degrade_with(
        team,
        ["budget-lab-primary", "budget-lab-degraded"],
        key_models=["all-team-models"],
    )

    assert applied["model"] == "budget-lab-degraded"


async def test_an_allowlisted_target_is_served(team):
    applied, _ = await degrade_with(team, ["budget-lab-primary", "budget-lab-degraded"])

    assert applied["model"] == "budget-lab-degraded"
    assert disclosed(applied)["outcome"] == "degraded"


async def test_an_empty_allowlist_permits_the_target(team):
    applied, _ = await degrade_with(team, [])

    assert applied["model"] == "budget-lab-degraded"


async def test_a_check_that_cannot_run_serves_the_primary_and_pages_the_platform(team):
    applied, sink = await degrade_with(
        team, [], permissions=bridge.LiteLLMPermissions(broken_router)
    )

    assert applied["model"] == "budget-lab-primary"
    assert [incident.reason for incident in sink.incidents] == [
        "entitlement_check_unavailable"
    ]
    assert sink.incidents[0].detail["target"] == "budget-lab-degraded"


async def test_the_unavailable_check_incident_is_raised_once_per_window(team):
    sink = RecordingSink()
    handler = build_handler(
        team,
        sink,
        spend=team.envelope_usd * 0.95,
        permissions=bridge.LiteLLMPermissions(broken_router),
    )

    for _ in range(3):
        await request(handler, make_auth(team), "budget-lab-primary")

    assert len(sink.incidents) == 1
