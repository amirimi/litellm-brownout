from __future__ import annotations

import contextlib
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import litellm
import pytest
from fastapi import HTTPException
from litellm.proxy.common_utils.timezone_utils import get_budget_reset_time

from brownout import bridge
from brownout.manifest import RoutePolicy, TeamPolicy
from brownout.policy import Permission, Rejection
from brownout.rules import ModelGroup

from .support import (
    FIELD_KEYS,
    FailingGuardrails,
    FakeGateway,
    FakeStore,
    FixedPermissions,
    RecordingPermissions,
    RecordingSink,
    SequenceSchedule,
    build_handler,
    disclosed,
    headers_for,
    make_auth,
    request,
)

TWO_WINDOWS = [datetime(2026, 9, 28, tzinfo=UTC), datetime(2026, 10, 5, tzinfo=UTC)]


def without_fallback_access(team: TeamPolicy):
    auth = make_auth(team)
    auth.team_models = ["budget-lab-primary", "budget-lab-strict"]
    return auth


async def refused_with(handler, auth, model: str, extra: dict[str, Any] | None = None):
    with pytest.raises(HTTPException) as raised:
        await request(handler, auth, model, extra)
    return raised.value


async def test_an_unpriced_model_fails_closed_and_pages_the_platform(team):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=100.0)

    error = await refused_with(handler, make_auth(team), "never-priced")

    assert error.status_code == 503
    assert error.detail["rule"] == "model-priced"
    assert [incident.as_payload()["reason"] for incident in sink.incidents] == [
        "unpriced_model"
    ]
    assert sink.incidents[0].as_payload()["owner"] == "platform"
    assert sink.tiers == []


async def test_made_up_model_names_raise_one_incident_per_team_per_window(team):
    sink = RecordingSink()
    store = FakeStore()
    handler = build_handler(team, sink, spend=100.0, store=store)

    for name in ("made-up-1", "made-up-2", "made-up-3"):
        assert (await refused_with(handler, make_auth(team), name)).status_code == 503

    assert [incident.as_payload()["model"] for incident in sink.incidents] == [
        "made-up-1"
    ]
    assert sink.incidents[0].as_payload()["model_group_served"] is False
    assert len(store.claimed) == 1


async def test_an_unpriced_model_group_the_gateway_serves_is_reported_per_group(team):
    sink = RecordingSink()
    gateway = FakeGateway.from_lab_config().serving(
        "served-unpriced-a", "served-unpriced-b"
    )
    handler = build_handler(team, sink, spend=100.0, gateway=gateway)

    for name in ("served-unpriced-a", "served-unpriced-b", "served-unpriced-a"):
        await refused_with(handler, make_auth(team), name)

    assert [
        (incident.as_payload()["model"], incident.as_payload()["model_group_served"])
        for incident in sink.incidents
    ] == [("served-unpriced-a", True), ("served-unpriced-b", True)]


async def test_an_unreadable_counter_refuses_and_pages_the_platform(team):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=RuntimeError("redis is down"))

    error = await refused_with(handler, make_auth(team), "budget-lab-primary")

    assert error.status_code == 503
    assert error.detail["reason"] == "spend_counter_unreadable"
    assert [incident.as_payload()["owner"] for incident in sink.incidents] == [
        "platform"
    ]
    assert sink.tiers == []


async def test_a_request_is_judged_without_its_own_reservation(team):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=950.0)

    applied = await request(
        handler, make_auth(team, reserved_cost=50.0), "budget-lab-primary"
    )

    field = disclosed(applied)
    assert [event.spend_usd for event in sink.tiers] == [900.0, 900.0]
    assert field["budget_tier"] == "90"
    assert field["served_route"] == "budget-lab-degraded"


async def test_a_request_does_not_degrade_on_the_size_of_its_own_hold(team):
    handler = build_handler(team, RecordingSink(), spend=930.0)

    applied = await request(
        handler, make_auth(team, reserved_cost=50.0), "budget-lab-primary"
    )

    field = disclosed(applied)
    assert field["served_route"] == "budget-lab-primary"
    assert field["outcome"] == "served"
    assert field["budget_tier"] == "75"
    assert applied["model"] == "budget-lab-primary"


async def test_holds_from_other_in_flight_requests_still_count(team):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=980.0)

    applied = await request(
        handler, make_auth(team, reserved_cost=50.0), "budget-lab-primary"
    )

    assert disclosed(applied)["outcome"] == "degraded"
    assert [event.spend_usd for event in sink.tiers] == [930.0, 930.0]


@pytest.mark.parametrize(
    ("reserved_cost", "applied_adjustment"), [(20.0, 0.0), (50.0, -30.0)]
)
async def test_a_hold_is_removed_at_the_size_it_now_holds(
    team, reserved_cost, applied_adjustment
):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=1000.0)

    await request(
        handler,
        make_auth(
            team, reserved_cost=reserved_cost, applied_adjustment=applied_adjustment
        ),
        "budget-lab-primary",
    )

    assert [event.spend_usd for event in sink.tiers] == [980.0, 980.0]


async def test_a_request_with_no_reservation_is_judged_on_the_counter_as_read(team):
    handler = build_handler(team, RecordingSink(), spend=930.0)

    applied = await request(handler, make_auth(team), "budget-lab-primary")

    assert disclosed(applied)["served_route"] == "budget-lab-degraded"


async def test_a_hold_larger_than_the_counter_floors_at_zero(team):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=10.0)

    applied = await request(
        handler, make_auth(team, reserved_cost=50.0), "budget-lab-primary"
    )

    assert "budget_tier" not in disclosed(applied)
    assert sink.tiers == []


@pytest.mark.parametrize(
    "interference",
    [
        {"fallbacks": ["budget-lab-degraded"]},
        {"mock_testing_fallbacks": True},
        {"context_window_fallbacks": [{"budget-lab-primary": ["budget-lab-degraded"]}]},
        {"content_policy_fallbacks": [{"budget-lab-primary": ["budget-lab-degraded"]}]},
    ],
)
async def test_caller_supplied_routing_is_refused_rather_than_ignored(
    team, interference
):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=10.0)

    error = await refused_with(
        handler, make_auth(team), "budget-lab-primary", interference
    )

    assert error.status_code == 400
    assert error.detail["error"] == "caller_routing_not_permitted"
    assert error.detail["refused"]
    assert sink.incidents == []


@pytest.mark.parametrize(
    "metadata",
    [
        {"metadata": {"x-brownout-route-served": "a caller tag"}},
        {"metadata": {"degraded_from": "a caller tag"}},
        {"litellm_metadata": {"x-brownout-budget-tier": "a caller tag"}},
    ],
)
async def test_caller_metadata_with_brownout_like_names_is_served(team, metadata):
    handler = build_handler(team, RecordingSink(), spend=10.0)

    applied = await request(handler, make_auth(team), "budget-lab-primary", metadata)

    assert disclosed(applied)["outcome"] == "served"


async def test_a_forged_field_is_overwritten_rather_than_refused(team):
    handler = build_handler(team, RecordingSink(), spend=10.0)
    forged = {
        "metadata": {
            "spend_logs_metadata": {
                "brownout": {"served_route": "forged", "outcome": "degraded"},
                "cost_center": "c-1",
            }
        }
    }

    applied = await request(handler, make_auth(team), "budget-lab-primary", forged)
    headers = await headers_for(handler, applied)

    assert disclosed(applied)["outcome"] == "served"
    assert applied["metadata"]["spend_logs_metadata"]["cost_center"] == "c-1"
    assert "x-brownout-degraded-from" not in headers
    assert headers["x-brownout-route-served"] == "budget-lab-primary"


async def test_a_forged_field_on_a_non_chat_call_is_removed(team):
    handler = build_handler(team, RecordingSink(), spend=10.0)
    forged = {
        "model": "embedding-model",
        "input": "hello",
        "metadata": {"spend_logs_metadata": {"brownout": {"served_route": "forged"}}},
    }

    data = await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team),
        cache=None,
        data=forged,
        call_type="aembedding",
    )

    assert data["metadata"]["spend_logs_metadata"] == {}
    assert bridge.headers(data, object(), None) is None


async def test_a_governed_request_discloses_only_facts_about_itself(team):
    handler = build_handler(team, RecordingSink(), spend=930.0)

    applied = await request(handler, make_auth(team), "budget-lab-primary")

    assert disclosed(applied) == {
        "requested_route": "budget-lab-primary",
        "outcome": "degraded",
        "served_route": "budget-lab-degraded",
        "budget_tier": "90",
        "degradation_reason": "degrade_tier_reached",
        "utilisation_percent": 93.0,
    }


@pytest.mark.parametrize(
    ("spend", "auth_change", "model"),
    [
        (10.0, {}, "budget-lab-primary"),
        (930.0, {}, "budget-lab-primary"),
        (930.0, {"team_models": ["budget-lab-primary"]}, "budget-lab-primary"),
        (100.0, {}, "never-priced"),
        (10.0, {"team_max_budget": 1.0}, "budget-lab-primary"),
    ],
)
async def test_the_field_never_names_anything_beyond_its_request(
    team, spend, auth_change, model
):
    handler = build_handler(team, RecordingSink(), spend=spend)
    auth = make_auth(team)
    for name, value in auth_change.items():
        setattr(auth, name, value)
    data: dict[str, Any] = {"model": model, "messages": []}

    with contextlib.suppress(HTTPException):
        await handler.async_pre_call_hook(
            user_api_key_dict=auth, cache=None, data=data, call_type="acompletion"
        )

    assert set(disclosed(data)) <= FIELD_KEYS


async def test_the_keys_own_spend_logs_metadata_is_never_mutated(team):
    handler = build_handler(team, RecordingSink(), spend=930.0)
    shared = {"cost_center": "k-1"}

    applied = await request(
        handler,
        make_auth(team),
        "budget-lab-primary",
        {"metadata": {"spend_logs_metadata": shared}},
    )

    assert shared == {"cost_center": "k-1"}
    assert applied["metadata"]["spend_logs_metadata"]["cost_center"] == "k-1"


async def test_a_refusal_discloses_its_reason_before_raising(team):
    handler = build_handler(team, RecordingSink(), spend=100.0)
    data: dict[str, Any] = {"model": "never-priced", "messages": []}

    with pytest.raises(HTTPException):
        await handler.async_pre_call_hook(
            user_api_key_dict=make_auth(team),
            cache=None,
            data=data,
            call_type="acompletion",
        )

    assert disclosed(data) == {
        "requested_route": "never-priced",
        "outcome": "refused",
        "refusal": "unpriced_model",
    }
    assert bridge.headers(data, object(), None) is None


async def test_a_caller_routing_refusal_discloses_no_incident(team):
    handler = build_handler(team, RecordingSink(), spend=10.0)
    data: dict[str, Any] = {
        "model": "budget-lab-primary",
        "messages": [],
        "fallbacks": ["budget-lab-degraded"],
    }

    with pytest.raises(HTTPException):
        await handler.async_pre_call_hook(
            user_api_key_dict=make_auth(team),
            cache=None,
            data=data,
            call_type="acompletion",
        )

    assert disclosed(data) == {
        "requested_route": "budget-lab-primary",
        "outcome": "refused",
        "refusal": "caller_routing_not_permitted",
    }


async def test_a_non_chat_call_is_passed_through_untouched(team):
    handler = build_handler(team, RecordingSink(), spend=999.0)
    original = {
        "model": "never-priced",
        "input": "hello",
        "fallbacks": ["caller-owned-fallback"],
        "metadata": {"x-brownout-caller-field": "not governed on this endpoint"},
    }

    data = await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team),
        cache=None,
        data=dict(original),
        call_type="aembedding",
    )

    assert data == original


async def test_an_envelope_that_no_longer_matches_the_team_ceiling_pages_the_platform(
    team,
):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=10.0)
    auth = make_auth(team)
    auth.team_max_budget = team.envelope_usd * 10

    error = await refused_with(handler, auth, "budget-lab-primary")

    payload = sink.incidents[0].as_payload()
    assert error.status_code == 503
    assert payload["reason"] == "budget_ceiling_drift"
    assert (payload["envelope_usd"], payload["team_max_budget_usd"]) == (
        1000.0,
        10000.0,
    )


async def test_a_missing_team_ceiling_fails_closed(team):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=10.0)
    auth = make_auth(team)
    auth.team_max_budget = None

    assert (await refused_with(handler, auth, "budget-lab-primary")).status_code == 503
    assert sink.incidents[0].detail["team_max_budget_usd"] is None


async def test_an_aligned_envelope_raises_no_incident(team):
    sink = RecordingSink()

    await request(
        build_handler(team, sink, spend=10.0), make_auth(team), "budget-lab-primary"
    )

    assert sink.incidents == []


@pytest.mark.parametrize(
    ("team_id", "message"),
    [
        pytest.param(
            "team-nobody-declared",
            "This key's team declares no budget in the brownout manifest, so its "
            "spend cannot be governed.",
            id="undeclared-team",
        ),
        pytest.param(
            None,
            "This key belongs to no team. Brownout governs team budgets only, so its "
            "spend cannot be governed.",
            id="no-team",
        ),
    ],
)
async def test_a_key_outside_the_manifest_fails_closed_and_pages_the_platform(
    team, team_id, message
):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=10.0)
    stranger = make_auth(team)
    stranger.team_id = team_id

    error = await refused_with(handler, stranger, "budget-lab-primary")

    assert error.status_code == 503
    assert error.detail["rule"] == "team-in-manifest"
    assert error.detail["message"] == message
    assert [incident.reason for incident in sink.incidents] == ["team_not_in_manifest"]


async def test_the_ungoverned_incident_is_raised_once_per_window(team):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=10.0)
    stranger = make_auth(team)
    stranger.team_id = "team-nobody-declared"

    for _ in range(3):
        await refused_with(handler, stranger, "budget-lab-primary")

    assert len(sink.incidents) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_budget", 5.0),
        ("model_max_budget", {"budget-lab-primary": {"max_budget": 1.0}}),
        ("budget_limits", [{"budget_limit": 1.0, "time_period": "1d"}]),
    ],
)
async def test_a_key_with_a_budget_of_its_own_is_refused(team, field, value):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=10.0)
    auth = make_auth(team)
    setattr(auth, field, value)

    error = await refused_with(handler, auth, "budget-lab-primary")

    assert error.status_code == 503
    assert error.detail["rule"] == "key-has-no-budget"
    assert error.detail["key_budgets"] == [field]
    assert [incident.reason for incident in sink.incidents] == ["key_has_own_budget"]


async def test_a_gateway_setting_violation_refuses_with_its_rule(team):
    sink = RecordingSink()
    gateway = FakeGateway.from_lab_config()
    gateway = gateway.with_settings(
        replace(gateway.current_settings, fail_closed_budget_enforcement=False)
    )
    handler = build_handler(team, sink, spend=10.0, gateway=gateway)

    error = await refused_with(handler, make_auth(team), "budget-lab-primary")

    assert error.status_code == 503
    assert (error.detail["reason"], error.detail["rule"]) == (
        "gateway_rule_violated",
        "fail-closed-on",
    )


def broken_pairing(kind: str) -> tuple[FakeGateway, str]:
    lab = FakeGateway.from_lab_config()
    match kind:
        case "removed":
            return lab.without_group("budget-lab-degraded"), "route-served"
        case "unpriced":
            unpriced = ModelGroup(
                name="budget-lab-degraded",
                input_cost_per_token=None,
                output_cost_per_token=None,
                data_classes=("internal",),
            )
            return lab.with_group(unpriced), "route-priced"
        case "leaky":
            pii_primary = ModelGroup(
                name="budget-lab-primary",
                input_cost_per_token=0.0001,
                output_cost_per_token=0.001,
                data_classes=("customer_pii",),
            )
            return lab.with_group(pii_primary), "fallback-data-class"
        case _:
            raise AssertionError(f"unknown pairing breakage {kind}")


@pytest.mark.parametrize("kind", ["removed", "unpriced", "leaky"])
async def test_a_broken_pairing_never_touches_a_request_below_the_threshold(team, kind):
    sink = RecordingSink()
    gateway, _ = broken_pairing(kind)
    handler = build_handler(team, sink, spend=10.0, gateway=gateway)

    applied = await request(handler, make_auth(team), "budget-lab-primary")

    assert applied["model"] == "budget-lab-primary"
    assert disclosed(applied)["outcome"] == "served"
    assert sink.incidents == []


@pytest.mark.parametrize("kind", ["removed", "unpriced", "leaky"])
async def test_a_broken_pairing_keeps_the_primary_and_pages_the_platform(team, kind):
    sink = RecordingSink()
    gateway, rule = broken_pairing(kind)
    permissions = RecordingPermissions(FixedPermissions(Permission.PERMITTED))
    handler = build_handler(
        team, sink, spend=950.0, gateway=gateway, permissions=permissions
    )

    applied = await request(handler, make_auth(team), "budget-lab-primary")

    assert applied["model"] == "budget-lab-primary"
    assert disclosed(applied)["outcome"] == "rejected"
    assert disclosed(applied)["degradation_reason"] == "fallback_rejected"
    assert [event.rejection for event in sink.fallback_rejections] == [
        Rejection.FALLBACK_RULE_VIOLATED
    ]
    assert [
        (incident.reason, incident.detail["rule"]) for incident in sink.incidents
    ] == [("gateway_rule_violated", rule)]
    assert permissions.checked == []


async def test_claims_carry_the_team_and_the_policy_epoch(team):
    store = FakeStore()
    handler = build_handler(team, RecordingSink(), spend=950.0, store=store)

    await request(handler, make_auth(team), "budget-lab-primary")

    assert sorted(key.split(":")[0] for key in store.claimed) == [
        "degraded",
        "tier",
        "tier",
    ]
    assert all(key.split(":")[1:3] == ["budget-lab", "test-1"] for key in store.claimed)


async def test_the_window_follows_the_gateways_reset_schedule(team, monkeypatch):
    monkeypatch.setattr(litellm, "timezone", "America/New_York", raising=False)
    monkeypatch.setattr(litellm, "budget_reset_time", "12:00", raising=False)
    store = FakeStore()
    handler = build_handler(
        team,
        RecordingSink(),
        spend=team.envelope_usd * 0.8,
        store=store,
        schedule=bridge.LiteLLMResetSchedule(),
    )

    await request(handler, make_auth(team), "budget-lab-primary")

    window = get_budget_reset_time(team.budget_duration).astimezone(UTC).isoformat()
    assert store.claimed == {f"tier:budget-lab:test-1:{window}:team:75"}


async def test_no_permission_check_runs_below_the_degrade_threshold(team):
    permissions = RecordingPermissions(FixedPermissions(Permission.PERMITTED))
    handler = build_handler(
        team, RecordingSink(), spend=team.envelope_usd * 0.8, permissions=permissions
    )

    await request(handler, make_auth(team), "budget-lab-primary")

    assert permissions.checked == []


async def test_a_broken_permission_check_below_the_threshold_pages_nobody(team):
    sink = RecordingSink()
    handler = build_handler(
        team,
        sink,
        spend=team.envelope_usd * 0.8,
        permissions=FixedPermissions(Permission.UNAVAILABLE),
    )

    await request(handler, make_auth(team), "budget-lab-primary")

    assert sink.incidents == []


async def test_a_rejected_downgrade_is_disclosed_to_the_caller_and_the_owner(team):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=team.envelope_usd * 0.95)

    applied = await request(
        handler, without_fallback_access(team), "budget-lab-primary"
    )
    headers = await headers_for(handler, applied)

    assert headers["x-brownout-route-served"] == "budget-lab-primary"
    assert "x-brownout-degraded-from" not in headers
    assert headers["x-brownout-degradation-reason"] == "fallback_rejected"
    assert [event.outcome for event in sink.tiers] == ["rejected", "rejected"]
    assert [event.rejection for event in sink.fallback_rejections] == [
        "outside_caller_allowlist"
    ]


async def test_an_ordinary_admission_discloses_no_degradation_reason(team):
    handler = build_handler(team, RecordingSink(), spend=10.0)

    headers = await headers_for(
        handler, await request(handler, make_auth(team), "budget-lab-primary")
    )

    assert "x-brownout-degradation-reason" not in headers
    assert "x-brownout-budget-tier" not in headers


async def test_nothing_is_claimed_when_the_rewrite_does_not_complete(team):
    sink = RecordingSink()
    store = FakeStore()
    handler = build_handler(
        team,
        sink,
        spend=team.envelope_usd * 0.95,
        store=store,
        guardrails=FailingGuardrails(),
    )

    with pytest.raises(RuntimeError):
        await request(handler, make_auth(team), "budget-lab-primary")

    assert sink.events == []
    assert store.claimed == set()


async def test_a_rejection_is_reported_after_another_route_claimed_the_tiers(team):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=team.envelope_usd * 0.95)
    auth = without_fallback_access(team)

    await request(handler, auth, "budget-lab-strict")
    await request(handler, auth, "budget-lab-primary")

    assert [(event.tier, event.route) for event in sink.tiers] == [
        (75, "budget-lab-strict"),
        (90, "budget-lab-strict"),
    ]
    assert [event.as_payload() for event in sink.fallback_rejections] == [
        {
            "event": "brownout_fallback_rejected",
            "owner": "team",
            "team_id": "budget-lab",
            "policy_epoch": "test-1",
            "window": sink.tiers[0].window,
            "route": "budget-lab-primary",
            "target": "budget-lab-degraded",
            "rejection": "outside_caller_allowlist",
            "degrade_at": 90,
            "spend_usd": 950.0,
            "envelope_usd": 1000.0,
        }
    ]


async def test_a_repeated_rejection_is_reported_once_per_window(team):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=team.envelope_usd * 0.95)

    await request(handler, without_fallback_access(team), "budget-lab-primary")
    await request(handler, without_fallback_access(team), "budget-lab-primary")

    assert len(sink.fallback_rejections) == 1


async def test_a_different_rejection_on_the_same_route_is_reported_separately(team):
    sink = RecordingSink()
    store = FakeStore()
    denied = build_handler(team, sink, spend=950.0, store=store)
    unavailable = build_handler(
        team,
        sink,
        spend=950.0,
        store=store,
        permissions=FixedPermissions(Permission.UNAVAILABLE),
    )

    await request(denied, without_fallback_access(team), "budget-lab-primary")
    await request(unavailable, make_auth(team), "budget-lab-primary")

    assert [event.rejection for event in sink.fallback_rejections] == [
        "outside_caller_allowlist",
        "entitlement_check_unavailable",
    ]
    assert [incident.reason for incident in sink.incidents] == [
        "entitlement_check_unavailable"
    ]


async def test_a_target_rejected_below_the_degrade_threshold_is_not_reported(team):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=team.envelope_usd * 0.8)

    await request(handler, without_fallback_access(team), "budget-lab-primary")

    assert [event.tier for event in sink.tiers] == [75]
    assert sink.fallback_rejections == []


async def test_a_new_window_reports_the_rejection_and_the_degradation_again(team):
    rejections = RecordingSink()
    rejecting = build_handler(
        team, rejections, spend=950.0, schedule=SequenceSchedule(list(TWO_WINDOWS))
    )
    degradations = RecordingSink()
    degrading = build_handler(
        team, degradations, spend=950.0, schedule=SequenceSchedule(list(TWO_WINDOWS))
    )

    for _ in TWO_WINDOWS:
        await request(rejecting, without_fallback_access(team), "budget-lab-primary")
        await request(degrading, make_auth(team), "budget-lab-primary")

    expected = ["2026-09-28T00:00:00+00:00", "2026-10-05T00:00:00+00:00"]
    assert [event.window for event in rejections.fallback_rejections] == expected
    assert [event.window for event in degradations.route_degradations] == expected


async def test_a_dearer_fallback_approved_in_the_manifest_is_served():
    route = RoutePolicy(
        route="chat-qa-degraded", fallback="chat-qa", degrade_at=90, warn_tiers=()
    )
    team = TeamPolicy(
        team_id="team-a",
        policy_epoch="team-a-v1",
        envelope_usd=500.0,
        budget_duration="7d",
        warn_tiers=(),
        routes={route.route: route},
    )
    sink = RecordingSink()

    applied = await request(
        build_handler(team, sink, spend=475.0), make_auth(team), "chat-qa-degraded"
    )

    assert applied["model"] == "chat-qa"
    assert sink.fallback_rejections == []


async def test_a_route_that_starts_degrading_is_announced(team):
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=team.envelope_usd * 0.95)

    await request(handler, make_auth(team), "budget-lab-primary")
    await request(handler, make_auth(team), "budget-lab-primary")

    assert [event.as_payload() for event in sink.route_degradations] == [
        {
            "event": "brownout_route_degraded",
            "owner": "team",
            "team_id": "budget-lab",
            "policy_epoch": "test-1",
            "window": sink.tiers[0].window,
            "route": "budget-lab-primary",
            "served_route": "budget-lab-degraded",
            "degrade_at": 90,
            "spend_usd": 950.0,
            "envelope_usd": 1000.0,
        }
    ]


async def test_each_degrading_route_is_announced_on_its_own(team):
    chat = RoutePolicy(
        route="chat-qa", fallback="chat-qa-degraded", degrade_at=90, warn_tiers=()
    )
    team = replace(team, routes={**team.routes, chat.route: chat})
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=team.envelope_usd * 0.95)

    await request(handler, make_auth(team), "budget-lab-strict")
    await request(handler, make_auth(team), "budget-lab-primary")
    await request(handler, make_auth(team), "chat-qa")

    assert {event.route for event in sink.tiers} == {"budget-lab-strict"}
    assert [(event.route, event.served_route) for event in sink.route_degradations] == [
        ("budget-lab-primary", "budget-lab-degraded"),
        ("chat-qa", "chat-qa-degraded"),
    ]


async def test_route_tiers_are_announced_for_their_route_alone(team):
    chat = RoutePolicy(
        route="budget-lab-strict", fallback=None, degrade_at=None, warn_tiers=(60, 80)
    )
    team = replace(team, warn_tiers=(75,), routes={**team.routes, chat.route: chat})
    sink = RecordingSink()
    handler = build_handler(team, sink, spend=team.envelope_usd * 0.85)

    primary = await request(handler, make_auth(team), "budget-lab-primary")
    strict = await request(handler, make_auth(team), "budget-lab-strict")

    assert [(event.scope, event.route, event.tier) for event in sink.tiers] == [
        ("team", "budget-lab-primary", 75),
        ("route", "budget-lab-strict", 60),
        ("route", "budget-lab-strict", 80),
    ]
    assert "x-brownout-route-tier" not in await headers_for(handler, primary)
    assert (await headers_for(handler, strict))["x-brownout-route-tier"] == "80"


async def test_a_degrade_threshold_raises_no_team_tier_of_its_own(team):
    route = RoutePolicy(
        route="budget-lab-primary",
        fallback="budget-lab-degraded",
        degrade_at=85,
        warn_tiers=(),
    )
    team = replace(team, warn_tiers=(75,), routes={route.route: route})
    sink = RecordingSink()

    await request(
        build_handler(team, sink, spend=team.envelope_usd * 0.86),
        make_auth(team),
        "budget-lab-primary",
    )

    assert [event.tier for event in sink.tiers] == [75]
    assert [event.degrade_at for event in sink.route_degradations] == [85]
