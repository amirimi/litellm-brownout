"""The platform-owned metric view, through the sink's own public methods.

Only derived operational metrics may reach Datadog, never PII or raw prompt or
response content, so the thing worth pinning is what the sink actually puts on
the wire. Every test here goes through ``tier`` or ``incident`` and
captures the submitted body, so a change to how a series is built cannot pass by
constructing the expected series in the test.
"""

from __future__ import annotations

import json
from typing import Any, Final

import httpx
import pytest

from budget_policy.adapter import _metric_sink
from budget_policy.metrics import (
    COUNT,
    GAUGE,
    METRIC_FALLBACK_REJECTED,
    METRIC_INCIDENT,
    METRIC_TIER_CROSSED,
    METRIC_UTILISATION,
    SUBMIT_TIMEOUT_SECONDS,
    DatadogMetricSink,
)
from budget_policy.signals import (
    FallbackRejectedEvent,
    IncidentEvent,
    Rejection,
    TierEvent,
)

from .support import RecordingEmitter, build_handler, make_auth

FORBIDDEN: Final = ("messages", "content", "prompt", "response", "user", "api_key")


def _tier_event(
    decision: str = "degrade", reason: str = "degrade_tier_reached"
) -> TierEvent:
    return TierEvent(
        team_id="budget-lab",
        policy_epoch="test-1",
        window="2026-08-31T00:00:00+00:00",
        tier=90,
        route="budget-lab-primary",
        served_route="budget-lab-degraded",
        decision=decision,
        reason=reason,
        spend_usd=0.9049,
        envelope_usd=1.0,
    )


class CapturingTransport(httpx.AsyncBaseTransport):
    """Answers the series API and keeps every request the sink made."""

    def __init__(self, status_code: int = 202) -> None:
        self.requests: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []
        self.timeouts: list[Any] = []
        self._status_code: Final = status_code

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers)
        self.timeouts.append(request.extensions.get("timeout"))
        return httpx.Response(self._status_code, json={"errors": []}, request=request)

    @property
    def series(self) -> list[dict[str, Any]]:
        return [entry for body in self.requests for entry in body["series"]]


def _sink_over(transport: httpx.AsyncBaseTransport, monkeypatch) -> DatadogMetricSink:
    """A real sink whose HTTP client answers from a transport the test can read.

    Substituting the transport rather than the submit method keeps the series
    construction, the header and the timeout inside what the test observes.
    """
    original: Final = httpx.AsyncClient

    def client(timeout: float) -> httpx.AsyncClient:
        return original(transport=transport, timeout=timeout)

    monkeypatch.setattr("budget_policy.metrics.httpx.AsyncClient", client)
    return DatadogMetricSink(api_key="k", site="datadoghq.eu", env="dev")


async def test_a_tier_submission_carries_only_numbers_and_tags(monkeypatch):
    """No message, response or credential can reach Datadog through this sink."""
    transport = CapturingTransport()
    sink = _sink_over(transport, monkeypatch)

    await sink.tier(_tier_event())

    flat = json.dumps(transport.requests)
    for forbidden in FORBIDDEN:
        assert forbidden not in flat, f"{forbidden!r} reached a Datadog series"
    assert all(
        isinstance(point["value"], float)
        for entry in transport.series
        for point in entry["points"]
    )


async def test_a_tier_submission_carries_the_declared_series_and_dimensions(
    monkeypatch,
):
    """Utilisation is a gauge on the team alone, so the dashboard can sum teams."""
    transport = CapturingTransport()
    sink = _sink_over(transport, monkeypatch)

    await sink.tier(_tier_event())

    submitted = {entry["metric"]: entry for entry in transport.series}
    assert set(submitted) == {METRIC_TIER_CROSSED, METRIC_UTILISATION}

    crossed = submitted[METRIC_TIER_CROSSED]
    assert crossed["type"] == COUNT
    assert set(crossed["tags"]) == {
        "env:dev",
        "service:budget-policy",
        "team:budget-lab",
        "tier:90",
        "decision:degrade",
        "reason:degrade_tier_reached",
        "route:budget-lab-primary",
    }

    utilisation = submitted[METRIC_UTILISATION]
    assert utilisation["type"] == GAUGE
    assert utilisation["points"][0]["value"] == pytest.approx(0.9049)
    assert set(utilisation["tags"]) == {
        "env:dev",
        "service:budget-policy",
        "team:budget-lab",
    }


async def test_a_rejected_downgrade_is_distinguishable_from_a_route_that_never_degrades(
    monkeypatch,
):
    """Both admit the primary. Only the tag separates a policy hole from a policy."""
    transport = CapturingTransport()
    sink = _sink_over(transport, monkeypatch)

    await sink.tier(_tier_event(decision="admit", reason="fallback_rejected"))

    crossed = next(
        entry for entry in transport.series if entry["metric"] == METRIC_TIER_CROSSED
    )
    assert "reason:fallback_rejected" in crossed["tags"]


async def test_a_rejection_submission_carries_the_declared_series_and_dimensions(
    monkeypatch,
):
    """One count per claimed rejection, tagged with the route and the reason."""
    transport = CapturingTransport()
    sink = _sink_over(transport, monkeypatch)

    await sink.fallback_rejected(
        FallbackRejectedEvent(
            team_id="budget-lab",
            policy_epoch="test-1",
            window="2026-08-31T00:00:00+00:00",
            route="budget-lab-primary",
            target="budget-lab-degraded",
            rejection=Rejection.OUTSIDE_CALLER_ALLOWLIST,
            tier=90,
            spend_usd=0.9049,
            envelope_usd=1.0,
        )
    )

    [rejected] = transport.series
    assert rejected["metric"] == METRIC_FALLBACK_REJECTED
    assert rejected["type"] == COUNT
    assert set(rejected["tags"]) == {
        "env:dev",
        "service:budget-policy",
        "team:budget-lab",
        "route:budget-lab-primary",
        "rejection:outside_caller_allowlist",
    }
    flat = json.dumps(transport.requests)
    for forbidden in FORBIDDEN:
        assert forbidden not in flat, f"{forbidden!r} reached a Datadog series"


async def test_an_incident_without_a_team_still_submits(monkeypatch):
    """A team is diagnostic context here, not a required dimension."""
    transport = CapturingTransport()
    sink = _sink_over(transport, monkeypatch)

    await sink.incident(
        IncidentEvent(reason="unpriced_model", window="w", detail={"model": "ghost"})
    )

    incident = next(
        entry for entry in transport.series if entry["metric"] == METRIC_INCIDENT
    )
    assert incident["type"] == COUNT
    assert set(incident["tags"]) == {
        "env:dev",
        "service:budget-policy",
        "reason:unpriced_model",
    }


async def test_a_submission_is_authenticated_and_time_bounded(monkeypatch):
    """A sink with no timeout would make a Datadog outage an inference outage."""
    transport = CapturingTransport()
    sink = _sink_over(transport, monkeypatch)

    await sink.tier(_tier_event())

    assert transport.headers[0]["DD-API-KEY"] == "k"
    assert transport.timeouts[0]["connect"] == SUBMIT_TIMEOUT_SECONDS


async def test_a_rejected_submission_is_logged_and_not_raised(monkeypatch, caplog):
    """Enforcement does not depend on this delivery, so it cannot fail a request."""
    transport = CapturingTransport(status_code=403)
    sink = _sink_over(transport, monkeypatch)

    await sink.tier(_tier_event())

    assert "Datadog metric submission rejected with status 403" in caplog.text


async def test_a_transport_failure_is_logged_and_not_raised(monkeypatch, caplog):
    class FailingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("datadog unreachable")

    sink = _sink_over(FailingTransport(), monkeypatch)

    await sink.tier(_tier_event())

    assert "Datadog metric submission" in caplog.text


async def test_a_tier_event_reaches_the_emitter_from_the_hook(team):
    """The platform-owned view is fed by the same decision the team sees."""
    emitter = RecordingEmitter()
    handler = build_handler(team=team, emitter=emitter, spend=950.0)

    await handler.async_pre_call_hook(
        user_api_key_dict=make_auth(team),
        cache=None,
        data={"model": "budget-lab-primary", "messages": []},
        call_type="acompletion",
    )

    assert [event.tier for event in emitter.tiers] == [75, 90]
    assert all(event.envelope_usd == team.envelope_usd for event in emitter.tiers)


def test_the_sink_is_absent_without_a_datadog_key(monkeypatch):
    """A lab with no Datadog configured stays quiet rather than erroring per event."""
    monkeypatch.delenv("DD_API_KEY", raising=False)
    assert _metric_sink() is None

    monkeypatch.setenv("DD_API_KEY", "k")
    monkeypatch.setenv("DD_SITE", "datadoghq.eu")
    assert _metric_sink() is not None
