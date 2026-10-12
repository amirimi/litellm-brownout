from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, assert_never

import httpx
import pytest

from brownout import bridge

from .support import FIELD_KEYS, REPO_ROOT, lab_spend_counter

GATEWAY: Final = os.getenv("LITELLM_GATEWAY", "http://localhost:4000")
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
ANNOUNCEMENT_LINE: Final = re.compile(r"brownout (\{.*\})\s*$")
DECLARED_ANNOUNCEMENTS: Final = frozenset(
    {"brownout_tier_crossed", "brownout_route_degraded", "brownout_incident"}
)
LOG_SETTLE_SECONDS: Final = 10.0
SPEND_LOG_WAIT_SECONDS: Final = 30.0
POLL_INTERVAL_SECONDS: Final = 1.0

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


@dataclass(frozen=True)
class LabRun:
    key: str
    started_at: str


@dataclass
class Observation:
    requests: int = 0
    first_degraded_headers: dict[str, str] | None = None
    blocked_status: int | None = None
    blocked_body: str = ""
    first_degraded_response_id: str | None = None


@dataclass(frozen=True)
class ForgedRequest:
    headers: dict[str, str]
    response_id: str


@pytest.fixture(scope="module")
def lab_run() -> LabRun:
    started_at = datetime.now(UTC).isoformat()
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "lab" / "setup_lab.py")],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"could not prepare the lab team: {result.stderr.strip()}")
    key = result.stdout.strip().splitlines()[-1]
    _require_zero_start()
    return LabRun(key=key, started_at=started_at)


@pytest.fixture(scope="module")
def forged(lab_run: LabRun) -> ForgedRequest:
    response = httpx.post(
        f"{GATEWAY}/v1/chat/completions",
        headers={"Authorization": f"Bearer {lab_run.key}"},
        json={
            "model": LAB_ROUTE,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": "hi"}],
            "metadata": {
                "spend_logs_metadata": {
                    "cost_center": "lab-forgery-check",
                    "brownout": {
                        "outcome": "degraded",
                        "served_route": "forged-route",
                        "team_id": "another-team",
                    },
                }
            },
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return ForgedRequest(
        headers={name.lower(): value for name, value in response.headers.items()},
        response_id=response.json()["id"],
    )


@pytest.fixture(scope="module")
def drained(lab_run: LabRun, forged: ForgedRequest) -> Observation:
    with httpx.Client(base_url=GATEWAY, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        return _drive_to_exhaustion(client, lab_run.key)


def _require_zero_start() -> None:
    response = httpx.get(
        f"{GATEWAY}/team/info",
        params={"team_id": LAB_TEAM_ID},
        headers=_admin_headers(),
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


def _admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}"}


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
            observed.first_degraded_response_id = response.json()["id"]
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


def _announcements(since: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + LOG_SETTLE_SECONDS
    previous: list[dict[str, Any]] | None = None
    while True:
        current = _read_announcements(since)
        if current == previous or time.monotonic() > deadline:
            return current
        previous = current
        time.sleep(POLL_INTERVAL_SECONDS)


def _read_announcements(since: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["docker", "compose", "logs", "--no-log-prefix", "--since", since, "litellm"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"could not read the gateway log: {result.stderr}")
    found = []
    for line in result.stdout.splitlines():
        match = ANNOUNCEMENT_LINE.search(line)
        if match is not None:
            found.append(json.loads(match.group(1)))
    return found


def _field_in_spend_logs(response_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + SPEND_LOG_WAIT_SECONDS
    while time.monotonic() < deadline:
        response = httpx.get(
            f"{GATEWAY}/spend/logs",
            params={"request_id": response_id},
            headers=_admin_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        for record in response.json():
            spend_logs_metadata = (record.get("metadata") or {}).get(
                "spend_logs_metadata"
            ) or {}
            if "brownout" in spend_logs_metadata:
                return spend_logs_metadata
        time.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(f"no spend log carried the brownout field for {response_id}")


def _team_tiers(announcements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event
        for event in announcements
        if event.get("event") == "brownout_tier_crossed"
        and event.get("team_id") == LAB_TEAM_ID
        and event.get("scope") == "team"
    ]


@requires_stack
def test_the_full_tier_sequence_is_observable_on_mock_routes(
    lab_run: LabRun, drained: Observation
) -> None:
    events = _team_tiers(_announcements(lab_run.started_at))
    tiers = [event["tier"] for event in events]

    assert tiers == [75, 90], (
        f"expected one 75 and one 90 announcement, saw {tiers} over "
        f"{drained.requests} requests"
    )
    assert events[0]["route"] == LAB_ROUTE
    assert events[0]["policy_epoch"] == "budget-lab-v1"
    assert events[0]["envelope_usd"] == LAB_ENVELOPE_USD
    assert events[0]["spend_usd"] >= 0.75
    assert events[1]["spend_usd"] >= 0.90
    assert all(event["owner"] == "team" for event in events)

    assert drained.first_degraded_headers is not None, (
        "no response disclosed a downgrade"
    )
    assert drained.first_degraded_headers["x-brownout-degraded-from"] == LAB_ROUTE
    assert drained.first_degraded_headers["x-brownout-route-served"] == DEGRADED_ROUTE
    assert drained.first_degraded_headers["x-brownout-budget-tier"] == "75"

    assert drained.blocked_status in BUDGET_REFUSAL_STATUSES, (
        f"never reached the native ceiling in {drained.requests} requests"
    )
    assert "budget" in drained.blocked_body.lower()

    release = _gateway_release()
    boundary = bridge.boundary_for(release)
    ceiling = _settled_ceiling_usd(boundary)
    counter = lab_spend_counter(LAB_TEAM_ID)
    assert counter <= ceiling, (
        f"the window closed with the counter at ${counter:.4f} against a "
        f"${ceiling:.4f} ceiling for the {boundary} boundary of LiteLLM {release}"
    )


@requires_stack
def test_the_gateway_log_carries_only_declared_announcements(
    lab_run: LabRun, drained: Observation
) -> None:
    observed = {event.get("event") for event in _announcements(lab_run.started_at)}

    assert "brownout_tier_crossed" in observed
    assert observed <= DECLARED_ANNOUNCEMENTS, (
        f"an undeclared announcement reached the gateway log: {observed}"
    )


@requires_stack
def test_the_downgrade_is_announced_once_for_the_route(
    lab_run: LabRun, drained: Observation
) -> None:
    degraded = [
        event
        for event in _announcements(lab_run.started_at)
        if event.get("event") == "brownout_route_degraded"
        and event.get("team_id") == LAB_TEAM_ID
    ]

    assert [
        (event["route"], event["served_route"], event["degrade_at"])
        for event in degraded
    ] == [(LAB_ROUTE, DEGRADED_ROUTE, 75)]


@requires_stack
def test_a_degraded_request_carries_its_facts_into_litellms_spend_logs(
    drained: Observation,
) -> None:
    assert drained.first_degraded_response_id is not None

    facts = _field_in_spend_logs(drained.first_degraded_response_id)["brownout"]

    assert facts["outcome"] == "degraded"
    assert facts["requested_route"] == LAB_ROUTE
    assert facts["served_route"] == DEGRADED_ROUTE
    assert facts["budget_tier"] == "75"
    assert set(facts) <= FIELD_KEYS


@requires_stack
def test_a_forged_field_is_replaced_on_a_request_the_plugin_handles(
    forged: ForgedRequest,
) -> None:
    spend_logs_metadata = _field_in_spend_logs(forged.response_id)

    assert forged.headers["x-brownout-route-served"] == LAB_ROUTE
    assert "x-brownout-degraded-from" not in forged.headers
    assert spend_logs_metadata["cost_center"] == "lab-forgery-check"
    assert spend_logs_metadata["brownout"]["outcome"] == "served"
    assert spend_logs_metadata["brownout"]["served_route"] == LAB_ROUTE
    assert set(spend_logs_metadata["brownout"]) <= FIELD_KEYS
