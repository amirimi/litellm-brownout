from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Final, assert_never

import httpx
import pytest

from brownout import bridge

from .support import REPO_ROOT, lab_spend_counter

GATEWAY: Final = os.getenv("LITELLM_GATEWAY", "http://localhost:4000")
EVENT_SINK: Final = os.getenv("LAB_EVENT_SINK", "http://localhost:8088/events")
LAB_TEAM_ID: Final = "budget-lab"
LAB_ROUTE: Final = "budget-lab-primary"
DEGRADED_ROUTE: Final = "budget-lab-degraded"
LAB_ENVELOPE_USD: Final = 1.0
LAB_PRIMARY_WORST_CASE_USD: Final = 0.0408
MAX_TOKENS: Final = 40
MAX_REQUESTS: Final = 80
REQUEST_TIMEOUT_SECONDS: Final = 30.0
BUDGET_REFUSAL_STATUSES: Final = frozenset({422, 429})
BUDGET_REFUSAL_MESSAGE: Final = "Budget has been exceeded"

pytestmark = pytest.mark.e2e


def _stack_is_up() -> bool:
    try:
        return httpx.get(f"{GATEWAY}/health/liveliness", timeout=3.0).status_code == 200
    except httpx.HTTPError:
        return False


def _stack_is_required() -> bool:
    return os.getenv("LAB_REQUIRE_STACK", "").lower() in {"1", "true", "yes"}


_STACK_UNAVAILABLE: Final = (
    "the composed gateway is not reachable, run `docker compose up -d` first"
)

if _stack_is_required() and not _stack_is_up():
    raise RuntimeError(f"LAB_REQUIRE_STACK is set and {_STACK_UNAVAILABLE}")

requires_stack = pytest.mark.skipif(not _stack_is_up(), reason=_STACK_UNAVAILABLE)


@pytest.fixture(scope="module")
def lab_key() -> str:
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
        if (
            response.status_code in BUDGET_REFUSAL_STATUSES
            and BUDGET_REFUSAL_MESSAGE in response.text
        ):
            observed.blocked_status = response.status_code
            observed.blocked_body = response.text
            return observed

        response.raise_for_status()
        headers = {name.lower(): value for name, value in response.headers.items()}
        if (
            observed.first_degraded_headers is None
            and "x-brownout-degraded-from" in headers
        ):
            observed.first_degraded_headers = headers
    return observed


def _gateway_release() -> str:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "litellm",
            "python",
            "-c",
            "from importlib.metadata import version; print(version('litellm'))",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"could not read the gateway's LiteLLM release: {result.stderr}")
    return result.stdout.strip()


def _settled_ceiling_usd(boundary: bridge.Boundary) -> float:
    match boundary:
        case "refuse_whole":
            return LAB_ENVELOPE_USD
        case "shrink_to_fit":
            return LAB_ENVELOPE_USD + LAB_PRIMARY_WORST_CASE_USD
        case _:
            assert_never(boundary)


def _tier_events(team_id: str = LAB_TEAM_ID) -> list[dict[str, Any]]:
    payload = httpx.get(EVENT_SINK, timeout=10.0).json()
    return [
        event
        for event in payload["events"]
        if event.get("event") == "brownout_tier_crossed"
        and event.get("team_id") == team_id
        and event.get("scope") == "team"
    ]


@requires_stack
def test_the_full_tier_sequence_is_observable_on_mock_routes(lab_key: str) -> None:
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
    assert observed.first_degraded_headers["x-brownout-degraded-from"] == LAB_ROUTE
    assert observed.first_degraded_headers["x-brownout-route-served"] == DEGRADED_ROUTE
    assert observed.first_degraded_headers["x-brownout-budget-tier"] == "75"

    assert observed.blocked_status in BUDGET_REFUSAL_STATUSES, (
        f"never reached the native ceiling in {observed.requests} requests"
    )
    assert "budget" in observed.blocked_body.lower()

    release = _gateway_release()
    boundary = bridge.boundary_for(release)
    ceiling = _settled_ceiling_usd(boundary)
    counter = lab_spend_counter(LAB_TEAM_ID)
    assert counter <= ceiling, (
        f"the window closed with the counter at ${counter:.4f} against a "
        f"${ceiling:.4f} ceiling for the {boundary} boundary of LiteLLM {release}"
    )


@requires_stack
def test_the_component_sink_contains_only_its_declared_event_types(
    lab_key: str,
) -> None:
    payload = httpx.get(EVENT_SINK, timeout=10.0).json()
    observed = [event.get("event") for event in payload["events"]]
    owners = set(observed)

    assert "brownout_tier_crossed" in owners, (
        f"the sequence crossed two tiers and the sink recorded {observed}"
    )
    assert owners <= {
        "brownout_tier_crossed",
        "brownout_route_degraded",
        "brownout_incident",
    }, f"an undeclared event type reached the component sink: {owners}"


@requires_stack
def test_the_downgrade_is_announced_once_for_the_route(lab_key: str) -> None:
    payload = httpx.get(EVENT_SINK, timeout=10.0).json()
    degraded = [
        event
        for event in payload["events"]
        if event.get("event") == "brownout_route_degraded"
        and event.get("team_id") == LAB_TEAM_ID
    ]

    assert [
        (event["route"], event["served_route"], event["degrade_at"])
        for event in degraded
    ] == [(LAB_ROUTE, DEGRADED_ROUTE, 75)]
