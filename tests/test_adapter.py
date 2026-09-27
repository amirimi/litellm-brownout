"""The LiteLLM seam: what the hook reads, refuses, rewrites and reports.

The spend counter and the tier store are faked because the adapter cannot
compute them. Everything else runs the real path, including the guardrail merge.
"""

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
    """The team gets no event. An unpriced model is a catalog gap the platform owns."""
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
    """A team is never paged for this."""
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
    """A tier measures consumption, and a hold is not consumption.

    LiteLLM reserves the worst case during authentication, so the counter this
    hook reads already contains a charge for a request that has not run. That
    hold is what the native ceiling at 100% is for. A tier asks a different
    question, how much of the envelope the team has consumed, and a request has
    consumed nothing at the moment it is being decided. A team at $900 of a
    $1000 envelope whose request reserves $50 reads $950 and is evaluated
    against $900.
    """
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
    """The distortion this removes, at its sharpest.

    Nothing else is in flight. Settled spend is $880 against a $1000 envelope,
    which is below the route's 90% degrade tier. The $50 hold alone pushes the
    counter to $930 and used to degrade the request that placed it, so the
    declared tier meant something the size of one reservation lower than it
    said.
    """
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
    """Only the request's own hold comes out.

    Settled spend is $500 and nine other admitted requests hold $50 each, so the
    counter reads $980 including this request's own $50. Those nine were already
    admitted and will each produce a real charge, so dropping them would let a
    burst sail past the tier together. This request is judged against $930.
    """
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
    """A reservation near the ceiling is resized down when fail-closed is off.

    ``_apply_over_budget_reservation_policy`` shrinks the hold to the budget
    that remains and admits, so the entry records a smaller figure than the
    worst case first estimated. With ``fail_closed_budget_enforcement`` on,
    LiteLLM 1.101.0 refuses that request instead, and this hook never sees it. Removing the original estimate would understate
    the team. Here $980 of settled spend leaves $20, the $50 estimate is resized
    to it, and the counter reads $1000.
    """
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
    """The helper is total over both fields that describe a hold.

    ``_set_reserved_entry_actual_cost`` records a change as
    ``applied_adjustment`` and leaves ``reserved_cost`` alone, so a reader of
    ``reserved_cost`` alone would be wrong by the adjustment. This pre-call hook
    never observes that state, because an admission-time resize normalises the
    entry back to ``applied_adjustment = 0``
    (``budget_reservation.py:979-981``) and reconciliation happens after the
    response. Summing both fields makes the helper correct by construction
    rather than correct by that timing argument.
    """
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
    """An unpriced route, or reservation disabled, leaves nothing to remove.

    ``reserve_budget_for_request`` returns None when it cannot estimate a worst
    case, and the counter then carries no hold for this request.
    """
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
    """The counter can legitimately read below this request's own hold.

    ``get_current_spend`` returns the recorded DB floor instead of the counter
    when a counter has drifted stale-low, and that floor carries no in-flight
    holds. Subtracting one from it would produce a negative utilisation, so the
    result is floored rather than trusted.
    """
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
    """Stripping caller routing silently was the worse failure.

    Stripping these silently meant a caller using per-request ``fallbacks`` saw
    them stop working with nothing to grep for. Refusing names the keys, so the
    caller learns on the first request instead of debugging silence.
    """
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
    """``_budget_policy`` is ours and ``_apply`` always rewrites it.

    A forged value can never be read by anything, so refusing the request would
    cost a caller a 400 for a key that carries no authority.
    """
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
    """The worst-case estimate is route-specific, so only chat completions are governed."""
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
    """The owner-approved budget and LiteLLM runtime ceiling must agree.

    Tiers are percentages of the budget and LiteLLM's native 429 owns 100%.
    Editing ``team.max_budget`` in the UI would silently move the hard stop away
    from every warning and degradation threshold without this check.
    """
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
    """A team without a native hard stop must not run on callback policy alone."""
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
    """A reviewed policy change re-arms transitions without changing the window."""
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

    assert len(store.claimed) == 2
    assert all(key.startswith("tier:budget-lab:test-1:") for key in store.claimed)


async def test_a_team_absent_from_the_manifest_pages_the_platform(team):
    """An undeclared team is ungoverned, and silence about that is the hole.

    It has no trustworthy envelope, action policy or price policy. The request
    fails closed and the incident makes the onboarding/configuration gap
    visible.
    """
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
    """A team that expected a downgrade and did not get one is owed the reason.

    The decision carries ``fallback_rejected`` either way. Without this it
    reached neither the caller's headers nor the tier event, which left a policy
    hole visible only in a per-request log line.
    """
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
    """The reason header means the degradation decision has something to say."""
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
    """A claim suppresses every later attempt in the window, so it must not
    outlive the request it describes.

    ``_apply`` re-runs the private guardrail merge against the new model and that
    merge can raise. Claiming the tier first would report a request as degraded to
    a served route when no provider dispatch ever happened, and would then silence
    the retry.
    """
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
    """A request crossing 75 and 90 must not pay two remote deadlines.

    Each tier is claimed separately, so deduplication is unchanged. Delivery is
    gathered, which holds the added latency at one round however many tiers a
    single request crosses.
    """
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
    """The plugin's window ends when LiteLLM's reset job says it does.

    LiteLLM stamps ``budget_reset_at`` with ``litellm_settings.timezone`` and
    ``budget_reset_time``. A window computed at UTC midnight instead would open
    hours before the counter resets and claim this window's tiers on last
    window's spend.
    """
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
    """A key whose team may call its routes and not the degradation target."""
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
    """A tier claim belongs to the team, so a route with no fallback can take it first.

    The rejection then reaches the owner on its own claim. Riding on the tier
    event alone, a downgrade that never happened stayed invisible until the 429.
    """
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
    """The claim carries the reason, so a second cause is not hidden by the first."""
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
    """Below ``degrade_at`` no downgrade was due, so none was refused."""
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
    """A request that crosses tiers and is rejected still pays one remote deadline."""

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
    """Manifest and catalog are fixed for the process, so a bad pairing stops startup.

    Checked per request, it surfaced only when a team reached its degrade tier,
    and as the team's event although the fix is the platform's configuration.
    """
    team = _team_with_one_route("chat-qa", "code-review")

    with pytest.raises(CatalogError, match="data class"):
        build_handler(team=team, emitter=RecordingEmitter(), spend=0.0)


async def test_a_dearer_fallback_approved_in_the_manifest_is_served(team):
    """Price is the route owner's call, so the plugin serves the declared target."""
    team = _team_with_one_route("chat-qa-degraded", "chat-qa")
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)

    applied = await _request(handler, make_auth(team), "chat-qa-degraded")

    assert applied["model"] == "chat-qa"
    assert emitter.fallback_rejections == []
