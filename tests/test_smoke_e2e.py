"""The production-shaped behavior, exercised against the composed stack over HTTP.

One scripted sequence on mock routes, so it spends no provider money and still
drives real reservations, a real Redis counter and the real native ceiling. It
observes the 75% and 90% warnings, a route-declared downgrade below the hard
ceiling, the 429 raised by LiteLLM at 100% of the owner-approved budget, and a
counter that never passed the envelope on the way there.

Skipped unless the stack is up. Bring it up with:

    docker compose up -d
    uv run pytest tests/test_smoke_e2e.py -v

Set ``LAB_REQUIRE_STACK=1`` where the sequence is the point, which turns an
unreachable gateway into a failure instead of a skip.

The sequence provisions the one lab team named in the manifest, so two copies
of it cannot run at once. Run this file serially.

The fixture runs `lab/setup_lab.py` itself and `conftest.py` loads `.env`,
so the master key does not have to be passed on the command line.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Final

import httpx
import pytest

from .support import REPO_ROOT, lab_spend_counter

GATEWAY: Final = os.getenv("LITELLM_GATEWAY", "http://localhost:4000")
EVENT_SINK: Final = os.getenv("LAB_EVENT_SINK", "http://localhost:8088/events")
LAB_TEAM_ID: Final = "budget-lab"
LAB_ROUTE: Final = "budget-lab-primary"
DEGRADED_ROUTE: Final = "budget-lab-degraded"
LAB_ENVELOPE_USD: Final = 1.0
MAX_TOKENS: Final = 40
MAX_REQUESTS: Final = 80
REQUEST_TIMEOUT_SECONDS: Final = 30.0

pytestmark = pytest.mark.e2e


def _stack_is_up() -> bool:
    try:
        return httpx.get(f"{GATEWAY}/health/liveliness", timeout=3.0).status_code == 200
    except httpx.HTTPError:
        return False


def _stack_is_required() -> bool:
    """Whether an unreachable gateway is a failure rather than a skip.

    A skip keeps the unit suite runnable from a clean clone with no Docker, and
    it is the wrong default anywhere the sequence is the point. CI sets this, so
    a stack that failed to come up cannot report as not applicable.
    """
    return os.getenv("LAB_REQUIRE_STACK", "").lower() in {"1", "true", "yes"}


_STACK_UNAVAILABLE: Final = (
    "the composed gateway is not reachable, run `docker compose up -d` first"
)

if _stack_is_required() and not _stack_is_up():
    raise RuntimeError(f"LAB_REQUIRE_STACK is set and {_STACK_UNAVAILABLE}")

requires_stack = pytest.mark.skipif(not _stack_is_up(), reason=_STACK_UNAVAILABLE)


@pytest.fixture(scope="module")
def lab_key() -> str:
    """A fresh lab team, envelope and key, so the sequence starts from zero.

    An unreachable gateway is a skip and is already handled by ``requires_stack``.
    A gateway that answered and then could not be set up is a failure, because
    turning that into a skip lets a broken stack report as not applicable.
    """
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "lab" / "setup_lab.py")],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"could not prepare the lab team: {result.stderr.strip()}")
    httpx.request("DELETE", EVENT_SINK, timeout=10.0)
    key = result.stdout.strip().splitlines()[-1]
    _require_zero_start(key)
    return key


def _require_zero_start(key: str) -> None:
    """Prove the envelope is empty before the sequence claims to have driven it.

    A run that starts mid-envelope produces the same empty event list as a run
    whose events never fired, and the second reading is the one worth chasing.
    Naming the dirty start separates them.
    """
    response = httpx.get(
        f"{GATEWAY}/team/info",
        params={"team_id": LAB_TEAM_ID},
        headers={"Authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    recorded = float(response.json()["team_info"].get("spend") or 0.0)
    assert recorded == 0.0, (
        f"the lab did not start from zero, recorded team spend is ${recorded:.4f}. "
        "LiteLLM's batched spend writer flushed the previous run onto the recreated team"
    )

    counter = lab_spend_counter(LAB_TEAM_ID)
    assert counter == 0.0, (
        f"the lab did not start from zero, the Redis counter holds ${counter:.4f}. "
        "The recorded row and the counter are different figures, and only the counter "
        "carries reservations, so both have to be clear before the sequence means anything"
    )


class Observation:
    """What the scripted sequence saw, in the order it saw it."""

    def __init__(self) -> None:
        self.requests = 0
        self.first_degraded_headers: dict[str, str] | None = None
        self.blocked_status: int | None = None
        self.blocked_body: str = ""


def _drive_to_exhaustion(client: httpx.Client, key: str) -> Observation:
    observed = Observation()
    for _ in range(MAX_REQUESTS):
        observed.requests += 1
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": LAB_ROUTE,
                "max_tokens": MAX_TOKENS,
                "messages": [
                    {"role": "user", "content": "Summarise the settlement window."}
                ],
            },
        )
        if response.status_code == 429:
            observed.blocked_status = response.status_code
            observed.blocked_body = response.text
            return observed

        response.raise_for_status()
        headers = {name.lower(): value for name, value in response.headers.items()}
        if (
            observed.first_degraded_headers is None
            and "x-platform-degraded-from" in headers
        ):
            observed.first_degraded_headers = headers
    return observed


def _tier_events(team_id: str = LAB_TEAM_ID) -> list[dict[str, Any]]:
    payload = httpx.get(EVENT_SINK, timeout=10.0).json()
    return [
        event
        for event in payload["events"]
        if event.get("event") == "budget_tier_crossed"
        and event.get("team_id") == team_id
    ]


@requires_stack
def test_the_full_tier_sequence_is_observable_on_mock_routes(lab_key: str) -> None:
    """The warning, downgrade and native hard-stop sequence is externally visible."""
    with httpx.Client(base_url=GATEWAY, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        observed = _drive_to_exhaustion(client, lab_key)

    events = _tier_events()
    tiers = [event["tier"] for event in events]

    assert tiers == [75, 90], (
        f"expected one 75 and one 90 event, saw {tiers} over {observed.requests} requests"
    )
    assert events[0]["route"] == LAB_ROUTE
    assert events[0]["policy_epoch"] == "budget-lab-v1"
    assert events[0]["envelope_usd"] == LAB_ENVELOPE_USD
    assert events[0]["spend_usd"] >= 0.75
    assert events[1]["spend_usd"] >= 0.90
    assert all(event["owner"] == "team" for event in events)

    assert observed.first_degraded_headers is not None, (
        "no response disclosed a downgrade"
    )
    assert observed.first_degraded_headers["x-platform-degraded-from"] == LAB_ROUTE
    assert observed.first_degraded_headers["x-platform-route-served"] == DEGRADED_ROUTE
    assert observed.first_degraded_headers["x-platform-budget-tier"] == "75"

    assert observed.blocked_status == 429, (
        f"never reached the native ceiling in {observed.requests} requests"
    )
    assert "budget" in observed.blocked_body.lower()

    counter = lab_spend_counter(LAB_TEAM_ID)
    assert counter <= LAB_ENVELOPE_USD, (
        f"the window closed with the counter at ${counter:.4f} against a "
        f"${LAB_ENVELOPE_USD:.2f} envelope, so a hold was admitted that did not fit. "
        "The lab config sets fail_closed_budget_enforcement, which refuses such a hold"
    )


@requires_stack
def test_the_component_sink_contains_only_its_declared_event_types(
    lab_key: str,
) -> None:
    """No undeclared payload shape reached the component-owned lab sink.

    The subset assertion alone is vacuously true on an empty sink, which is
    exactly the state a broken emitter produces. The sequence crossed 75 and 90,
    so the tier event has to be present before the subset means anything.
    """
    payload = httpx.get(EVENT_SINK, timeout=10.0).json()
    observed = [event.get("event") for event in payload["events"]]
    owners = set(observed)

    assert "budget_tier_crossed" in owners, (
        f"the sequence crossed two tiers and the sink recorded {observed}"
    )
    assert owners <= {"budget_tier_crossed", "budget_policy_incident"}, (
        f"an undeclared event type reached the component sink: {owners}"
    )
