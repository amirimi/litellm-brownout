"""Entitlement for a degradation target the caller never asked for.

The allowlist is enforced during authentication against the model the caller
named. A rewrite after that point is never re-checked, so this component asks
LiteLLM's own check again rather than keeping a second copy of the rule.
"""

from __future__ import annotations

from .support import (
    RecordingEmitter,
    build_handler,
    make_auth,
)


async def test_a_degradation_target_outside_the_team_allowlist_is_not_selected(team):
    """A downgrade must not route around the team's model entitlement.

    LiteLLM checks the allowlist during authentication against the model the
    caller named. The rewrite happens after that and is never re-checked, so
    without this the component could serve a model the team is not permitted to
    use. Serving the primary costs a downgrade rather than granting access.
    """
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)
    auth = make_auth(team)
    auth.team_models = ["budget-lab-primary", "budget-lab-strict"]

    data = await handler.async_pre_call_hook(
        user_api_key_dict=auth,
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    assert data["model"] == "budget-lab-primary"
    disclosed = data["metadata"]["_budget_policy"]
    assert disclosed["served_route"] == "budget-lab-primary"
    assert disclosed["degraded_from"] is None
    assert disclosed["reason"] == "fallback_rejected"


async def test_a_target_outside_the_key_allowlist_is_not_selected(team):
    """A key can be narrower than its team, and the rewrite must respect that.

    LiteLLM authorises the requested model against the key's own list before the
    team's (``can_key_call_model``). Checking only ``team_models`` would let a
    downgrade serve a model the key is forbidden to call, which a direct request
    to that model would have refused with a 403.
    """
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)
    auth = make_auth(team)
    auth.team_models = ["budget-lab-primary", "budget-lab-degraded"]
    auth.models = ["budget-lab-primary"]

    data = await handler.async_pre_call_hook(
        user_api_key_dict=auth,
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    assert data["model"] == "budget-lab-primary"
    disclosed = data["metadata"]["_budget_policy"]
    assert disclosed["served_route"] == "budget-lab-primary"
    assert disclosed["reason"] == "fallback_rejected"


async def test_a_key_inheriting_its_team_allowlist_may_be_degraded(team):
    """``all-team-models`` on a key means it inherits the team's list.

    Resolving that sentinel is what stops the key check from reading an
    inherited allowlist as a one-entry literal list and refusing every downgrade
    (``auth_checks.py:4059``).
    """
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)
    auth = make_auth(team)
    auth.team_models = ["budget-lab-primary", "budget-lab-degraded"]
    auth.models = ["all-team-models"]

    data = await handler.async_pre_call_hook(
        user_api_key_dict=auth,
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    assert data["model"] == "budget-lab-degraded"


async def test_an_allowlisted_degradation_target_is_selected(team):
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)
    auth = make_auth(team)
    auth.team_models = ["budget-lab-primary", "budget-lab-degraded"]

    data = await handler.async_pre_call_hook(
        user_api_key_dict=auth,
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    assert data["model"] == "budget-lab-degraded"
    assert data["metadata"]["_budget_policy"]["degraded_from"] == "budget-lab-primary"


async def test_an_empty_allowlist_still_permits_the_degradation_target(team):
    """An empty team model list means unrestricted in LiteLLM, not forbidden."""
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)
    auth = make_auth(team)
    auth.team_models = []

    data = await handler.async_pre_call_hook(
        user_api_key_dict=auth,
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    assert data["model"] == "budget-lab-degraded"


def _broken_router() -> None:
    raise RuntimeError("router not initialised")


async def test_a_broken_entitlement_check_pages_the_platform_rather_than_denying(
    team, monkeypatch
):
    """A denial and a check that never ran need different responses.

    ``_can_object_call_model`` raises ``ProxyException`` to say no. Anything else
    means the router or the auth path is broken, and treating that as an ordinary
    narrow allowlist would serve the primary at full price with nothing to alert
    on. Both refuse the target and only one is a platform fault.
    """
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)

    monkeypatch.setattr("budget_policy.adapter.router", _broken_router)

    data = await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team),
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    assert data["model"] == "budget-lab-primary"
    assert [event.reason for event in emitter.incidents] == [
        "entitlement_check_unavailable"
    ]
    assert emitter.incidents[0].detail["target"] == "budget-lab-degraded"


async def test_the_entitlement_incident_is_raised_once_per_window(team, monkeypatch):
    """A per-request page is a page nobody reads."""
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=team.envelope_usd * 0.95)

    monkeypatch.setattr("budget_policy.adapter.router", _broken_router)

    for _ in range(3):
        await handler.async_pre_call_hook(
            user_api_key_dict=make_auth(team),
            cache=None,
            data={"model": "budget-lab-primary", "messages": []},
            call_type="acompletion",
        )

    assert len(emitter.incidents) == 1
