from __future__ import annotations

import asyncio
from typing import Any, Final

import pytest

from brownout import bridge
from brownout.events import Event, EventEmitter, IncidentEvent, TierEvent, TierScope

TTL_SECONDS: Final = 1209600


def tier_event() -> TierEvent:
    return TierEvent(
        team_id="budget-lab",
        policy_epoch="test-1",
        window="2026-08-24T00:00:00+00:00",
        scope=TierScope.TEAM,
        route="budget-lab-primary",
        tier=75,
        served_route="budget-lab-degraded",
        outcome="degraded",
        spend_usd=0.75,
        envelope_usd=1.0,
    )


class FakeRedisCache:
    def __init__(self, fail: bool = False) -> None:
        self.keys: dict[str, Any] = {}
        self.ttls: dict[str, int] = {}
        self._fail: Final = fail

    async def async_set_cache(self, key: str, value: Any, nx: bool, ttl: int) -> bool:
        if self._fail:
            raise RuntimeError("redis unavailable")
        assert nx is True, "a claim must never overwrite a key it did not win"
        if key in self.keys:
            return False
        self.keys[key] = value
        self.ttls[key] = ttl
        return True


def store_backed_by(cache: Any) -> bridge.RedisClaimStore:
    return bridge.RedisClaimStore(lambda: cache)


async def test_only_one_replica_wins_a_claim():
    cache = FakeRedisCache()

    assert await store_backed_by(cache).claim("tier:a", TTL_SECONDS) is True
    assert await store_backed_by(cache).claim("tier:a", TTL_SECONDS) is False


async def test_a_claim_is_namespaced_and_carries_the_window_ttl():
    cache = FakeRedisCache()

    await store_backed_by(cache).claim("tier:budget-lab:test-1:w:team:75", TTL_SECONDS)

    assert cache.ttls == {"brownout:tier:budget-lab:test-1:w:team:75": TTL_SECONDS}


async def test_an_unreachable_redis_falls_back_to_replica_local_dedup(caplog):
    store = store_backed_by(FakeRedisCache(fail=True))

    assert await store.claim("tier:a", TTL_SECONDS) is True
    assert await store.claim("tier:a", TTL_SECONDS) is False
    assert "replica-local" in caplog.text


async def test_a_missing_redis_warns_once(caplog):
    store = store_backed_by(None)

    for tier in (75, 90, 95):
        await store.claim(f"tier:{tier}", TTL_SECONDS)

    assert caplog.text.count("Deduplication is replica-local") == 1


async def test_a_redis_lookup_that_raises_counts_as_missing():
    def broken() -> None:
        raise RuntimeError("proxy not running")

    assert await bridge.RedisClaimStore(broken).claim("tier:a", TTL_SECONDS) is True


async def test_webhook_and_metric_delivery_start_concurrently(monkeypatch):
    webhook_started = asyncio.Event()
    metric_started = asyncio.Event()

    class CoordinatedSink:
        async def submit(self, event: Event) -> None:
            metric_started.set()
            await asyncio.wait_for(webhook_started.wait(), timeout=0.2)

    emitter = EventEmitter("https://events.invalid", CoordinatedSink())

    async def coordinated_post(payload: dict[str, Any]) -> None:
        webhook_started.set()
        await asyncio.wait_for(metric_started.wait(), timeout=0.2)

    monkeypatch.setattr(emitter, "_post", coordinated_post)

    await emitter.emit(tier_event())

    assert webhook_started.is_set() and metric_started.is_set()


async def test_a_failed_remote_sink_does_not_fail_the_request(caplog):
    class FailingSink:
        async def submit(self, event: Event) -> None:
            raise RuntimeError("sink unavailable")

    await EventEmitter(None, FailingSink()).emit(tier_event())

    assert "Brownout event remote sink failed" in caplog.text


async def test_cancellation_is_not_swallowed_as_a_sink_failure():
    class CancelledSink:
        async def submit(self, event: Event) -> None:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await EventEmitter(None, CancelledSink()).emit(tier_event())


async def test_team_events_log_at_warning_and_incidents_at_error(caplog):
    emitter = EventEmitter(None, None)

    await emitter.emit(tier_event())
    await emitter.emit(IncidentEvent(reason="unpriced_model", window="w", detail={}))

    assert [record.levelname for record in caplog.records] == ["WARNING", "ERROR"]
    assert all(record.name == "brownout" for record in caplog.records)
