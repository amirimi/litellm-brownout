"""The atomic claim that deduplicates a transition, and bounded remote delivery.

The claim is the only thing that stops one crossing becoming one event per
replica. Its key carries the team, policy epoch, budget window and tier, so a
window reset or an approved policy change re-arms every tier without anyone
guessing how long a warning should live.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

import pytest

from budget_policy.signals import (
    EventEmitter,
    IncidentEvent,
    TierEvent,
    TierSignalStore,
)

TTL_SECONDS: Final = 1209600


def _tier_event() -> TierEvent:
    return TierEvent(
        team_id="budget-lab",
        policy_epoch="test-1",
        window="2026-08-24T00:00:00+00:00",
        tier=75,
        route="budget-lab-primary",
        served_route="budget-lab-degraded",
        decision="degrade",
        reason="degrade_tier_reached",
        spend_usd=0.75,
        envelope_usd=1.0,
    )


class FakeRedisCache:
    """``SET NX`` semantics, and nothing else the store is allowed to rely on."""

    def __init__(self, fail: bool = False) -> None:
        self.keys: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self._fail: Final = fail

    async def async_set_cache(self, key: str, value: Any, nx: bool, ttl: int) -> bool:
        if self._fail:
            raise RuntimeError("redis unavailable")
        assert nx is True, "the claim must never overwrite a key it did not win"
        if key in self.keys:
            return False
        self.keys[key] = value
        self.ttls[key] = ttl
        return True


def _store_backed_by(cache: Any, monkeypatch: pytest.MonkeyPatch) -> TierSignalStore:
    store = TierSignalStore()
    monkeypatch.setattr(store, "_redis_cache", lambda: cache)
    return store


async def test_only_one_caller_wins_a_claim(monkeypatch):
    """This is what makes a crossing one event rather than one event per replica."""
    cache = FakeRedisCache()
    first = _store_backed_by(cache, monkeypatch)
    second = _store_backed_by(cache, monkeypatch)

    key = "tier:budget-lab:test-1:2026-08-24T00:00:00+00:00:75"

    assert await first.claim(key, TTL_SECONDS) is True
    assert await second.claim(key, TTL_SECONDS) is False


async def test_a_new_window_rearms_every_tier(monkeypatch):
    """The window is part of the claim key.

    The window is part of the key, so nothing has to expire for the next window
    to warn again. A claim settled in one window does not answer for the next.
    """
    cache = FakeRedisCache()
    store = _store_backed_by(cache, monkeypatch)

    settled = "tier:budget-lab:test-1:2026-08-24T00:00:00+00:00:75"
    assert await store.claim(settled, TTL_SECONDS) is True
    assert await store.claim(settled, TTL_SECONDS) is False

    next_window = "tier:budget-lab:test-1:2026-08-31T00:00:00+00:00:75"
    assert await store.claim(next_window, TTL_SECONDS) is True


async def test_an_approved_policy_change_rearms_every_tier(monkeypatch):
    """A team whose envelope was raised is owed its warnings against the new one."""
    cache = FakeRedisCache()
    store = _store_backed_by(cache, monkeypatch)

    assert await store.claim("tier:budget-lab:epoch-1:w:75", TTL_SECONDS) is True
    assert await store.claim("tier:budget-lab:epoch-2:w:75", TTL_SECONDS) is True


async def test_a_claim_is_namespaced_and_carries_the_window_ttl(monkeypatch):
    """Correctness comes from the key. The expiry only stops Redis accumulating."""
    cache = FakeRedisCache()
    store = _store_backed_by(cache, monkeypatch)

    await store.claim("tier:budget-lab:test-1:w:75", TTL_SECONDS)

    assert list(cache.keys) == ["budget_policy:tier:budget-lab:test-1:w:75"]
    assert cache.ttls["budget_policy:tier:budget-lab:test-1:w:75"] == TTL_SECONDS


async def test_an_unreachable_redis_falls_back_to_replica_local_dedup(
    monkeypatch, caplog
):
    """Losing Redis must not turn one crossing into one event per request."""
    store = _store_backed_by(FakeRedisCache(fail=True), monkeypatch)

    assert await store.claim("tier:budget-lab:test-1:w:75", TTL_SECONDS) is True
    assert await store.claim("tier:budget-lab:test-1:w:75", TTL_SECONDS) is False
    assert "replica-local" in caplog.text


async def test_the_replica_local_warning_is_logged_once(monkeypatch, caplog):
    """A per-request warning would be the noise this component exists to remove."""
    store = _store_backed_by(None, monkeypatch)

    for tier in (75, 90, 95):
        await store.claim(f"tier:budget-lab:test-1:w:{tier}", TTL_SECONDS)

    assert caplog.text.count("Deduplication is replica-local") == 1


async def test_webhook_and_metric_delivery_start_concurrently(monkeypatch):
    webhook_started = asyncio.Event()
    metric_started = asyncio.Event()

    class CoordinatedMetricSink:
        async def tier(self, event: TierEvent) -> None:
            metric_started.set()
            await asyncio.wait_for(webhook_started.wait(), timeout=0.2)

        async def incident(self, event: IncidentEvent) -> None:
            raise AssertionError("not used")

    emitter = EventEmitter("https://events.invalid", CoordinatedMetricSink())

    async def coordinated_post(payload: dict[str, Any]) -> None:
        webhook_started.set()
        await asyncio.wait_for(metric_started.wait(), timeout=0.2)

    monkeypatch.setattr(emitter, "_post", coordinated_post)

    await emitter.emit_tier(_tier_event())

    assert webhook_started.is_set()
    assert metric_started.is_set()


async def test_remote_sink_failure_does_not_fail_the_request(caplog):
    class FailingMetricSink:
        async def tier(self, event: TierEvent) -> None:
            raise RuntimeError("sink unavailable")

        async def incident(self, event: IncidentEvent) -> None:
            raise RuntimeError("sink unavailable")

    emitter = EventEmitter(None, FailingMetricSink())

    await emitter.emit_tier(_tier_event())

    assert "Budget event remote sink failed" in caplog.text


async def test_cancellation_is_not_swallowed_as_a_sink_failure():
    class CancelledMetricSink:
        async def tier(self, event: TierEvent) -> None:
            raise asyncio.CancelledError

        async def incident(self, event: IncidentEvent) -> None:
            raise AssertionError("not used")

    emitter = EventEmitter(None, CancelledMetricSink())

    with pytest.raises(asyncio.CancelledError):
        await emitter.emit_tier(_tier_event())
