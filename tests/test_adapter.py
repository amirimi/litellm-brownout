from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import litellm
import pytest
from fastapi import HTTPException
from litellm.proxy.common_utils.timezone_utils import get_budget_reset_time

from budget_policy.catalog import CatalogError
from budget_policy.manifest import Posture, RoutePolicy, TeamPolicy

from .support import (
    FakeStore,
    RecordingEmitter,
    build_handler,
    make_auth,
)


async def test_an_unpriced_model_fails_closed_and_pages_the_platform(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=100.0)

    with pytest.raises(HTTPException) as raised:
        await handler.async_pre_call_hook(
            user_api_key_dict=make_auth(team),
            cache=None,
            data={"model": "never-priced", "messages": []},
            call_type="acompletion",
        )

    assert raised.value.status_code == 503
    assert len(emitter.incidents) == 1
    incident = emitter.incidents[0].as_payload()
    assert incident["reason"] == "unpriced_model"
    assert incident["owner"] == "platform"
    assert emitter.tiers == []


async def test_an_unreadable_counter_raises_a_platform_incident(team):
    emitter = RecordingEmitter()
    handler = build_handler(
        team=team, emitter=emitter, spend=RuntimeError("redis is down")
    )

    with pytest.raises(HTTPException) as raised:
        await handler.async_pre_call_hook(
            user_api_key_dict=make_auth(team),
            cache=None,
            data={"model": "budget-lab-primary", "messages": []},
            call_type="acompletion",
        )

    assert raised.value.status_code == 503
    assert [incident.as_payload()["owner"] for incident in emitter.incidents] == [
        "platform"
    ]
    assert emitter.tiers == []


async def test_a_request_is_judged_without_its_own_reservation(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=950.0)

    data = await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team, reserved_cost=50.0),
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    disclosed = data["metadata"]["_budget_policy"]
    assert [event.as_payload()["spend_usd"] for event in emitter.tiers] == [
        900.0,
        900.0,
    ]
    assert disclosed["tier"] == 90
    assert disclosed["served_route"] == "budget-lab-degraded"


async def test_a_request_does_not_degrade_on_the_size_of_its_own_hold(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=930.0)

    data = await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team, reserved_cost=50.0),
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    disclosed = data["metadata"]["_budget_policy"]
    assert disclosed["served_route"] == "budget-lab-primary"
    assert disclosed["degraded_from"] is None
    assert disclosed["tier"] == 75
    assert data["model"] == "budget-lab-primary"


async def test_holds_from_other_in_flight_requests_still_count(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=980.0)

    data = await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team, reserved_cost=50.0),
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    disclosed = data["metadata"]["_budget_policy"]
    assert disclosed["served_route"] == "budget-lab-degraded"
    assert disclosed["degraded_from"] == "budget-lab-primary"
    assert [event.as_payload()["spend_usd"] for event in emitter.tiers] == [
        930.0,
        930.0,
    ]


async def test_a_resized_reservation_is_removed_at_the_size_it_now_holds(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=1000.0)

    await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team, reserved_cost=20.0),
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    assert [event.as_payload()["spend_usd"] for event in emitter.tiers] == [
        980.0,
        980.0,
    ]


async def test_an_adjusted_entry_is_read_at_its_adjusted_size(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=1000.0)

    await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team, reserved_cost=50.0, applied_adjustment=-30.0),
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    assert [event.as_payload()["spend_usd"] for event in emitter.tiers] == [
        980.0,
        980.0,
    ]


async def test_a_request_with_no_reservation_is_judged_on_the_counter_as_read(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=930.0)

    data = await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team),
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    assert data["metadata"]["_budget_policy"]["served_route"] == "budget-lab-degraded"


async def test_a_hold_larger_than_the_counter_floors_at_zero(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=10.0)

    data = await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team, reserved_cost=50.0),
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    disclosed = data["metadata"]["_budget_policy"]
    assert disclosed["served_route"] == "budget-lab-primary"
    assert disclosed["tier"] is None
    assert emitter.tiers == []


@pytest.mark.parametrize(
    "interference",
    [
        {"fallbacks": ["budget-lab-degraded"]},
        {"mock_testing_fallbacks": True},
        {"metadata": {"x-platform-route-served": "something-expensive"}},
        {"metadata": {"degraded_from": "something-expensive"}},
        {"litellm_metadata": {"x-platform-budget-tier": "10"}},
    ],
)
async def test_caller_supplied_routing_is_refused_rather_than_ignored(
    team, interference
):
    handler = build_handler(team=team, emitter=RecordingEmitter(), spend=10.0)
    data: dict[str, Any] = {
        "model": "budget-lab-primary",
        "messages": [],
        **interference,
    }

    with pytest.raises(HTTPException) as raised:
        await handler.async_pre_call_hook(
            user_api_key_dict=make_auth(team),
            cache=None,
            data=data,
            call_type="acompletion",
        )

    assert raised.value.status_code == 400
    assert raised.value.detail["error"] == "caller_routing_not_permitted"
    assert raised.value.detail["refused"]


async def test_a_forged_disclosure_is_overwritten_rather_than_refused(team):
    handler = build_handler(team=team, emitter=RecordingEmitter(), spend=10.0)
    data = {
        "model": "budget-lab-primary",
        "messages": [],
        "metadata": {
            "_budget_policy": {
                "served_route": "forged",
                "degraded_from": "forged",
                "tier": 99,
                "reason": "forged",
            }
        },
    }

    applied = await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team),
        cache=None,
        data=data,
        call_type="acompletion",
    )

    assert applied["metadata"]["_budget_policy"]["served_route"] == "budget-lab-primary"
    assert applied["metadata"]["_budget_policy"]["degraded_from"] is None

    headers = await handler.async_post_call_response_headers_hook(
        data=applied,
        user_api_key_dict=make_auth(team),
        response=None,
    )
    assert "x-platform-degraded-from" not in headers
    assert headers["x-platform-route-served"] == "budget-lab-primary"


async def test_a_non_chat_route_is_passed_through_untouched(team):
    handler = build_handler(team=team, emitter=RecordingEmitter(), spend=999.0)
    original = {
        "model": "never-priced",
        "input": "hello",
        "fallbacks": ["caller-owned-fallback"],
        "metadata": {"x-platform-caller-field": "not ours on this endpoint"},
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
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=10.0)
    auth = make_auth(team)
    auth.team_max_budget = team.envelope_usd * 10

    with pytest.raises(HTTPException) as raised:
        await handler.async_pre_call_hook(
            user_api_key_dict=auth,
            cache=None,
            data={"model": "budget-lab-primary", "messages": []},
            call_type="acompletion",
        )

    payloads = [incident.as_payload() for incident in emitter.incidents]
    assert raised.value.status_code == 503
    assert [payload["reason"] for payload in payloads] == ["budget_ceiling_drift"]
    assert payloads[0]["owner"] == "platform"
    assert payloads[0]["envelope_usd"] == 1000.0
    assert payloads[0]["team_max_budget_usd"] == 10000.0


async def test_an_aligned_envelope_raises_no_incident(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=10.0)

    await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team),
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    assert emitter.incidents == []


async def test_a_missing_native_team_budget_fails_closed(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=10.0)
    auth = make_auth(team)
    auth.team_max_budget = None

    with pytest.raises(HTTPException) as raised:
        await handler.async_pre_call_hook(
            user_api_key_dict=auth,
            cache=None,
            data={"model": "budget-lab-primary", "messages": []},
            call_type="acompletion",
        )

    assert raised.value.status_code == 503
    assert emitter.incidents[0].reason == "budget_ceiling_drift"
    assert emitter.incidents[0].detail["team_max_budget_usd"] is None


async def test_tier_claims_include_the_policy_epoch(team):
    store = FakeStore()
    handler = build_handler(
        team=team,
        emitter=RecordingEmitter(),
        spend=950.0,
        store=store,
    )

    await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team),
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    assert sorted(key.split(":")[0] for key in store.claimed) == [
        "degraded",
        "tier",
        "tier",
    ]
    assert all(key.split(":")[1:3] == ["budget-lab", "test-1"] for key in store.claimed)


async def test_a_team_absent_from_the_manifest_pages_the_platform(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=10.0)
    stranger = make_auth(team)
    stranger.team_id = "team-nobody-declared"

    with pytest.raises(HTTPException) as raised:
        await handler.async_pre_call_hook(
            user_api_key_dict=stranger,
            cache=None,
            data={"model": "budget-lab-primary", "messages": []},
            call_type="acompletion",
        )

    payloads = [incident.as_payload() for incident in emitter.incidents]
    assert [payload["reason"] for payload in payloads] == ["team_not_in_manifest"]
    assert payloads[0]["team_id"] == "team-nobody-declared"
    assert payloads[0]["owner"] == "platform"
    assert raised.value.status_code == 503


async def test_a_key_without_team_attribution_fails_closed_and_pages_the_platform(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=10.0)
    unattributed = make_auth(team)
    unattributed.team_id = None

    with pytest.raises(HTTPException) as raised:
        await handler.async_pre_call_hook(
            user_api_key_dict=unattributed,
            cache=None,
            data={"model": "budget-lab-primary", "messages": []},
            call_type="acompletion",
        )

    assert raised.value.status_code == 503
    assert emitter.incidents[0].as_payload()["reason"] == "team_not_in_manifest"


async def test_the_ungoverned_incident_is_raised_once_per_window(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=10.0)
    stranger = make_auth(team)
    stranger.team_id = "team-nobody-declared"

    for _ in range(3):
        with pytest.raises(HTTPException):
            await handler.async_pre_call_hook(
                user_api_key_dict=stranger,
                cache=None,
                data={"model": "budget-lab-primary", "messages": []},
                call_type="acompletion",
            )

    assert len(emitter.incidents) == 1


async def test_a_rejected_downgrade_is_disclosed_to_the_caller_and_the_platform(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)
    auth = make_auth(team)
    auth.team_models = ["budget-lab-primary"]

    applied = await handler.async_pre_call_hook(
        user_api_key_dict=auth,
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )
    headers = await handler.async_post_call_response_headers_hook(
        data=applied,
        user_api_key_dict=auth,
        response=None,
    )

    assert headers["x-platform-route-served"] == "budget-lab-primary"
    assert "x-platform-degraded-from" not in headers
    assert headers["x-platform-degradation-reason"] == "fallback_rejected"
    assert [event.reason for event in emitter.tiers] == [
        "fallback_rejected",
        "fallback_rejected",
    ]


async def test_an_ordinary_admission_discloses_no_degradation_reason(team):
    handler = build_handler(team=team, emitter=RecordingEmitter(), spend=10.0)

    applied = await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team),
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )
    headers = await handler.async_post_call_response_headers_hook(
        data=applied,
        user_api_key_dict=make_auth(team),
        response=None,
    )

    assert "x-platform-degradation-reason" not in headers


async def test_no_tier_is_claimed_when_the_rewrite_does_not_complete(team, monkeypatch):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)

    def failing_merge(data, llm_router, trust_client_model_info):
        raise RuntimeError("guardrail merge failed")

    monkeypatch.setattr(
        "budget_policy.adapter._check_and_merge_model_level_guardrails", failing_merge
    )

    with pytest.raises(RuntimeError):
        await handler.async_pre_call_hook(
            user_api_key_dict=make_auth(team),
            cache=None,
            data={"model": "budget-lab-primary", "messages": []},
            call_type="acompletion",
        )

    assert emitter.tiers == []


async def test_tiers_crossed_together_are_delivered_together(team):
    started = 0
    peak = 0

    class CountingEmitter(RecordingEmitter):
        async def emit_tier(self, event):
            nonlocal started, peak
            started += 1
            peak = max(peak, started)
            await asyncio.sleep(0)
            started -= 1
            await super().emit_tier(event)

    emitter = CountingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)

    await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team),
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    assert [event.tier for event in emitter.tiers] == [75, 90]
    assert peak == 2, "the two tier events were delivered one after the other"


async def test_the_window_follows_the_gateways_reset_schedule(team, monkeypatch):
    monkeypatch.setattr(litellm, "timezone", "America/New_York", raising=False)
    monkeypatch.setattr(litellm, "budget_reset_time", "12:00", raising=False)
    store = FakeStore()
    handler = build_handler(
        team=team,
        emitter=RecordingEmitter(),
        spend=team.envelope_usd * 0.8,
        store=store,
    )

    await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team),
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    window = get_budget_reset_time(team.budget_duration).astimezone(UTC).isoformat()
    assert store.claimed == {f"tier:{team.team_id}:{team.policy_epoch}:{window}:75"}


def _without_fallback_access(team):
    auth = make_auth(team)
    auth.team_models = ["budget-lab-primary", "budget-lab-strict"]
    return auth


async def _request(handler, auth, model: str) -> dict:
    return await handler.async_pre_call_hook(
        user_api_key_dict=auth,
        cache=None,
        data={"model": model, "messages": []},
        call_type="acompletion",
    )


async def test_a_rejected_downgrade_is_reported_after_another_route_claimed_the_tiers(
    team,
):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)
    auth = _without_fallback_access(team)

    await _request(handler, auth, "budget-lab-strict")
    await _request(handler, auth, "budget-lab-primary")

    assert [(event.tier, event.route) for event in emitter.tiers] == [
        (75, "budget-lab-strict"),
        (90, "budget-lab-strict"),
    ]
    assert [event.as_payload() for event in emitter.fallback_rejections] == [
        {
            "event": "budget_fallback_rejected",
            "owner": "team",
            "team_id": "budget-lab",
            "policy_epoch": "test-1",
            "window": emitter.tiers[0].window,
            "route": "budget-lab-primary",
            "target": "budget-lab-degraded",
            "rejection": "outside_caller_allowlist",
            "tier": 90,
            "spend_usd": 950.0,
            "envelope_usd": 1000.0,
        }
    ]


async def test_a_repeated_rejection_is_reported_once_per_window(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)
    auth = _without_fallback_access(team)

    await _request(handler, auth, "budget-lab-primary")
    await _request(handler, auth, "budget-lab-primary")

    assert [event.rejection for event in emitter.fallback_rejections] == [
        "outside_caller_allowlist"
    ]


async def test_a_different_rejection_on_the_same_route_is_reported_separately(
    team, monkeypatch
):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)

    await _request(handler, _without_fallback_access(team), "budget-lab-primary")

    def check_that_cannot_run(model, llm_router, models, team_id, object_type):
        raise RuntimeError("router state unavailable")

    monkeypatch.setattr(
        "budget_policy.adapter._can_object_call_model", check_that_cannot_run
    )
    await _request(handler, make_auth(team), "budget-lab-primary")

    assert [event.rejection for event in emitter.fallback_rejections] == [
        "outside_caller_allowlist",
        "entitlement_check_unavailable",
    ]
    assert [incident.reason for incident in emitter.incidents] == [
        "entitlement_check_unavailable"
    ]


async def test_a_target_rejected_below_the_degrade_tier_is_not_reported(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.8)

    await _request(handler, _without_fallback_access(team), "budget-lab-primary")

    assert [event.tier for event in emitter.tiers] == [75]
    assert emitter.fallback_rejections == []


async def test_a_new_window_reports_the_rejection_again(team, monkeypatch):
    windows = iter(
        [datetime(2026, 9, 28, tzinfo=UTC), datetime(2026, 10, 5, tzinfo=UTC)]
    )
    monkeypatch.setattr(
        "budget_policy.adapter.get_budget_reset_time",
        lambda budget_duration: next(windows),
    )
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)
    auth = _without_fallback_access(team)

    await _request(handler, auth, "budget-lab-primary")
    await _request(handler, auth, "budget-lab-primary")

    assert [event.window for event in emitter.fallback_rejections] == [
        "2026-09-28T00:00:00+00:00",
        "2026-10-05T00:00:00+00:00",
    ]


async def test_tier_and_rejection_events_are_delivered_together(team):

    class CountingEmitter(RecordingEmitter):
        def __init__(self) -> None:
            super().__init__()
            self.in_flight = 0
            self.peak = 0

        async def _overlap(self) -> None:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            await asyncio.sleep(0)
            self.in_flight -= 1

        async def emit_tier(self, event):
            await self._overlap()
            await super().emit_tier(event)

        async def emit_fallback_rejected(self, event):
            await self._overlap()
            await super().emit_fallback_rejected(event)

    emitter = CountingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)

    await _request(handler, _without_fallback_access(team), "budget-lab-primary")

    assert [event.tier for event in emitter.tiers] == [75, 90]
    assert len(emitter.fallback_rejections) == 1
    assert emitter.peak == 3, "the rejection was delivered after the tier events"


def _team_with_one_route(route: str, fallback: str) -> TeamPolicy:
    policy = RoutePolicy(
        route=route,
        fallback=fallback,
        degrade_at=90,
        on_counter_unavailable=Posture.BLOCK,
    )
    return TeamPolicy(
        team_id="team-a",
        policy_epoch="team-a-v1",
        envelope_usd=500.0,
        budget_duration="7d",
        warn_tiers=(75, 90),
        default_posture=Posture.BLOCK,
        routes={route: policy},
    )


def test_the_handler_refuses_a_fallback_the_catalog_cannot_allow():
    team = _team_with_one_route("chat-qa", "code-review")

    with pytest.raises(CatalogError, match="data class"):
        build_handler(team=team, emitter=RecordingEmitter(), spend=0.0)


async def test_a_dearer_fallback_approved_in_the_manifest_is_served(team):
    team = _team_with_one_route("chat-qa-degraded", "chat-qa")
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)

    applied = await _request(handler, make_auth(team), "chat-qa-degraded")

    assert applied["model"] == "chat-qa"
    assert emitter.fallback_rejections == []


async def test_a_route_that_starts_degrading_is_announced(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)

    await _request(handler, make_auth(team), "budget-lab-primary")

    assert [event.as_payload() for event in emitter.route_degradations] == [
        {
            "event": "budget_route_degraded",
            "owner": "team",
            "team_id": "budget-lab",
            "policy_epoch": "test-1",
            "window": emitter.tiers[0].window,
            "route": "budget-lab-primary",
            "served_route": "budget-lab-degraded",
            "tier": 90,
            "spend_usd": 950.0,
            "envelope_usd": 1000.0,
        }
    ]


async def test_a_degrading_route_is_announced_once_per_window(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)

    await _request(handler, make_auth(team), "budget-lab-primary")
    await _request(handler, make_auth(team), "budget-lab-primary")

    assert len(emitter.route_degradations) == 1


async def test_each_degrading_route_is_announced_on_its_own(team):
    chat = RoutePolicy(
        route="chat-qa",
        fallback="chat-qa-degraded",
        degrade_at=90,
        on_counter_unavailable=Posture.BLOCK,
    )
    team = TeamPolicy(
        team_id=team.team_id,
        policy_epoch=team.policy_epoch,
        envelope_usd=team.envelope_usd,
        budget_duration=team.budget_duration,
        warn_tiers=team.warn_tiers,
        default_posture=team.default_posture,
        routes={**team.routes, chat.route: chat},
    )
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)

    await _request(handler, make_auth(team), "budget-lab-primary")
    await _request(handler, make_auth(team), "chat-qa")

    assert [
        (event.route, event.served_route) for event in emitter.route_degradations
    ] == [
        ("budget-lab-primary", "budget-lab-degraded"),
        ("chat-qa", "chat-qa-degraded"),
    ]


async def test_a_route_degrading_after_another_claimed_the_tier_is_announced(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)

    await _request(handler, make_auth(team), "budget-lab-strict")
    await _request(handler, make_auth(team), "budget-lab-primary")

    assert {event.route for event in emitter.tiers} == {"budget-lab-strict"}
    assert [event.route for event in emitter.route_degradations] == [
        "budget-lab-primary"
    ]


async def test_a_new_window_announces_the_degrading_route_again(team, monkeypatch):
    windows = iter(
        [datetime(2026, 9, 28, tzinfo=UTC), datetime(2026, 10, 5, tzinfo=UTC)]
    )
    monkeypatch.setattr(
        "budget_policy.adapter.get_budget_reset_time",
        lambda budget_duration: next(windows),
    )
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)

    await _request(handler, make_auth(team), "budget-lab-primary")
    await _request(handler, make_auth(team), "budget-lab-primary")

    assert [event.window for event in emitter.route_degradations] == [
        "2026-09-28T00:00:00+00:00",
        "2026-10-05T00:00:00+00:00",
    ]


async def test_no_degradation_is_announced_when_the_rewrite_does_not_complete(
    team, monkeypatch
):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)

    def failing_merge(data, llm_router, trust_client_model_info):
        raise RuntimeError("guardrail merge failed")

    monkeypatch.setattr(
        "budget_policy.adapter._check_and_merge_model_level_guardrails", failing_merge
    )

    with pytest.raises(RuntimeError):
        await _request(handler, make_auth(team), "budget-lab-primary")

    assert emitter.route_degradations == []


async def test_tier_and_degradation_events_are_delivered_together(team):

    class CountingEmitter(RecordingEmitter):
        def __init__(self) -> None:
            super().__init__()
            self.in_flight = 0
            self.peak = 0

        async def _overlap(self) -> None:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            await asyncio.sleep(0)
            self.in_flight -= 1

        async def emit_tier(self, event):
            await self._overlap()
            await super().emit_tier(event)

        async def emit_route_degraded(self, event):
            await self._overlap()
            await super().emit_route_degraded(event)

    emitter = CountingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)

    await _request(handler, make_auth(team), "budget-lab-primary")

    assert [event.tier for event in emitter.tiers] == [75, 90]
    assert len(emitter.route_degradations) == 1
    assert emitter.peak == 3, "the degradation was delivered after the tier events"


async def test_made_up_model_names_raise_one_incident_per_team_per_window(team):
    emitter = RecordingEmitter()
    store = FakeStore()
    handler = build_handler(team=team, emitter=emitter, spend=100.0, store=store)

    for name in ("made-up-1", "made-up-2", "made-up-3"):
        with pytest.raises(HTTPException) as raised:
            await _request(handler, make_auth(team), name)
        assert raised.value.status_code == 503

    assert [incident.as_payload()["model"] for incident in emitter.incidents] == [
        "made-up-1"
    ]
    assert emitter.incidents[0].as_payload()["model_group_served"] is False
    assert len(store.claimed) == 1


async def test_an_unpriced_model_group_the_gateway_serves_is_reported_per_group(
    team, monkeypatch
):

    class ServingRouter:
        def get_model_names(self, team_id: str | None = None) -> list[str]:
            return ["served-unpriced-a", "served-unpriced-b"]

    monkeypatch.setattr("budget_policy.adapter.router", lambda: ServingRouter())
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=100.0)

    for name in ("served-unpriced-a", "served-unpriced-b", "served-unpriced-a"):
        with pytest.raises(HTTPException):
            await _request(handler, make_auth(team), name)

    assert [
        (incident.as_payload()["model"], incident.as_payload()["model_group_served"])
        for incident in emitter.incidents
    ] == [("served-unpriced-a", True), ("served-unpriced-b", True)]
