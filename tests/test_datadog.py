from __future__ import annotations

import json
from typing import Any, Final

import httpx
import pytest

from brownout import settings
from brownout.datadog import (
    COUNT,
    GAUGE,
    METRIC_FALLBACK_REJECTED,
    METRIC_INCIDENT,
    METRIC_ROUTE_DEGRADED,
    METRIC_TIER_CROSSED,
    METRIC_UTILISATION,
    SUBMIT_TIMEOUT_SECONDS,
    DatadogSink,
)
from brownout.events import (
    FallbackRejectedEvent,
    IncidentEvent,
    RouteDegradedEvent,
    TierEvent,
    TierScope,
)
from brownout.policy import Rejection

FORBIDDEN: Final = ("messages", "content", "prompt", "response", "user", "api_key")


def tier_event(scope: TierScope = TierScope.TEAM) -> TierEvent:
    return TierEvent(
        team_id="budget-lab",
        policy_epoch="test-1",
        window="2026-08-31T00:00:00+00:00",
        scope=scope,
        route="budget-lab-primary",
        tier=90,
        served_route="budget-lab-degraded",
        outcome="degraded",
        spend_usd=0.9049,
        envelope_usd=1.0,
    )


class CapturingTransport(httpx.AsyncBaseTransport):
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


def sink_over(transport: httpx.AsyncBaseTransport, monkeypatch) -> DatadogSink:
    original: Final = httpx.AsyncClient

    def client(timeout: float) -> httpx.AsyncClient:
        return original(transport=transport, timeout=timeout)

    monkeypatch.setattr("brownout.datadog.httpx.AsyncClient", client)
    return DatadogSink(api_key="k", site="datadoghq.eu", env="dev")


def tags_of(transport: CapturingTransport, metric: str) -> set[str]:
    return set(
        next(entry for entry in transport.series if entry["metric"] == metric)["tags"]
    )


async def test_a_submission_carries_only_numbers_and_tags(monkeypatch):
    transport = CapturingTransport()

    await sink_over(transport, monkeypatch).submit(tier_event())

    flat = json.dumps(transport.requests)
    assert not [forbidden for forbidden in FORBIDDEN if forbidden in flat]
    assert all(
        isinstance(point["value"], float)
        for entry in transport.series
        for point in entry["points"]
    )


async def test_a_team_tier_carries_a_count_and_the_utilisation_gauge(monkeypatch):
    transport = CapturingTransport()

    await sink_over(transport, monkeypatch).submit(tier_event())

    submitted = {entry["metric"]: entry for entry in transport.series}
    assert set(submitted) == {METRIC_TIER_CROSSED, METRIC_UTILISATION}
    assert submitted[METRIC_TIER_CROSSED]["type"] == COUNT
    assert set(submitted[METRIC_TIER_CROSSED]["tags"]) == {
        "env:dev",
        "service:brownout",
        "team:budget-lab",
        "tier:90",
        "scope:team",
        "outcome:degraded",
    }
    assert submitted[METRIC_UTILISATION]["type"] == GAUGE
    assert submitted[METRIC_UTILISATION]["points"][0]["value"] == pytest.approx(0.9049)


async def test_a_route_tier_carries_its_route_and_no_gauge(monkeypatch):
    transport = CapturingTransport()

    await sink_over(transport, monkeypatch).submit(tier_event(TierScope.ROUTE))

    assert [entry["metric"] for entry in transport.series] == [METRIC_TIER_CROSSED]
    assert {"scope:route", "route:budget-lab-primary"} <= tags_of(
        transport, METRIC_TIER_CROSSED
    )


async def test_a_degradation_carries_the_route_and_the_served_route(monkeypatch):
    transport = CapturingTransport()

    await sink_over(transport, monkeypatch).submit(
        RouteDegradedEvent(
            team_id="budget-lab",
            policy_epoch="test-1",
            window="w",
            route="budget-lab-primary",
            served_route="budget-lab-degraded",
            degrade_at=90,
            spend_usd=0.9,
            envelope_usd=1.0,
        )
    )

    assert tags_of(transport, METRIC_ROUTE_DEGRADED) == {
        "env:dev",
        "service:brownout",
        "team:budget-lab",
        "route:budget-lab-primary",
        "served_route:budget-lab-degraded",
    }


async def test_a_rejection_carries_the_route_and_the_reason(monkeypatch):
    transport = CapturingTransport()

    await sink_over(transport, monkeypatch).submit(
        FallbackRejectedEvent(
            team_id="budget-lab",
            policy_epoch="test-1",
            window="w",
            route="budget-lab-primary",
            target="budget-lab-degraded",
            rejection=Rejection.OUTSIDE_CALLER_ALLOWLIST,
            degrade_at=90,
            spend_usd=0.9,
            envelope_usd=1.0,
        )
    )

    assert tags_of(transport, METRIC_FALLBACK_REJECTED) == {
        "env:dev",
        "service:brownout",
        "team:budget-lab",
        "route:budget-lab-primary",
        "rejection:outside_caller_allowlist",
    }


async def test_an_incident_without_a_team_still_submits(monkeypatch):
    transport = CapturingTransport()

    await sink_over(transport, monkeypatch).submit(
        IncidentEvent(reason="unpriced_model", window="w", detail={"model": "ghost"})
    )

    assert tags_of(transport, METRIC_INCIDENT) == {
        "env:dev",
        "service:brownout",
        "reason:unpriced_model",
    }


async def test_a_submission_is_authenticated_and_time_bounded(monkeypatch):
    transport = CapturingTransport()

    await sink_over(transport, monkeypatch).submit(tier_event())

    assert transport.headers[0]["DD-API-KEY"] == "k"
    assert transport.timeouts[0]["connect"] == SUBMIT_TIMEOUT_SECONDS


async def test_a_rejected_submission_is_logged_and_not_raised(monkeypatch, caplog):
    await sink_over(CapturingTransport(status_code=403), monkeypatch).submit(
        tier_event()
    )

    assert "Datadog metric submission rejected with status 403" in caplog.text


async def test_a_transport_failure_is_logged_and_not_raised(monkeypatch, caplog):
    class FailingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("datadog unreachable")

    await sink_over(FailingTransport(), monkeypatch).submit(tier_event())

    assert "Datadog metric submission" in caplog.text


def test_the_sink_exists_only_with_a_datadog_key(monkeypatch):
    monkeypatch.delenv("DD_API_KEY", raising=False)
    assert settings.datadog_sink() is None

    monkeypatch.setenv("DD_API_KEY", "k")
    assert settings.datadog_sink() is not None
