from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, assert_never

import httpx
import pytest
from litellm.proxy._types import hash_token

from brownout import bridge
from brownout.manifest import Manifest

from .support import FIELD_KEYS, REPO_ROOT, lab_spend_counter

GATEWAY: Final = os.getenv("LITELLM_GATEWAY", "http://localhost:4000")
LAB_TEAM_ID: Final = "budget-lab"
LAB_ROUTE: Final = "budget-lab-primary"
DEGRADED_ROUTE: Final = "budget-lab-degraded"
FAILING_ROUTE: Final = "budget-lab-failing"
LAB_ENVELOPE_USD: Final = 1.0
LAB_PRIMARY_WORST_CASE_USD: Final = 0.0408
MAX_TOKENS: Final = 40
MAX_REQUESTS: Final = 80
REQUEST_TIMEOUT_SECONDS: Final = 30.0
BUDGET_REFUSAL_STATUSES: Final = frozenset({422, 429})
BUDGET_REFUSAL_MESSAGE: Final = "Budget has been exceeded"
ANNOUNCEMENT_LINE: Final = re.compile(r"brownout (\{.*\})\s*$")
FINDING_LINE: Final = re.compile(r"^([a-z-]+)\t([^\t]+)\t")
LAB_CONFIG: Final = REPO_ROOT / "config" / "config.yaml"
LAB_MANIFEST: Final = REPO_ROOT / "config" / "manifest.yaml"
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
    disclosure_mismatches: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class Answer:
    status_code: int
    headers: dict[str, str]
    body: str


@dataclass(frozen=True)
class ForgedRequest:
    headers: dict[str, str]
    response_id: str


@pytest.fixture(scope="module")
def lab_run() -> LabRun:
    started_at = datetime.now(UTC).isoformat()
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tests" / "setup_lab.py")],
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
def team_fallback(lab_run: LabRun) -> Answer:
    _set_team_router_settings(
        {"fallbacks": [{FAILING_ROUTE: [LAB_ROUTE]}]},
    )
    try:
        return _ask(lab_run.key, {"model": FAILING_ROUTE})
    finally:
        _set_team_router_settings({})


@pytest.fixture(scope="module")
def caller_fallback(lab_run: LabRun) -> Answer:
    return _ask(
        lab_run.key,
        {
            "model": LAB_ROUTE,
            "context_window_fallbacks": [{LAB_ROUTE: [DEGRADED_ROUTE]}],
        },
    )


@pytest.fixture(scope="module")
def drained(
    lab_run: LabRun,
    forged: ForgedRequest,
    team_fallback: Answer,
    caller_fallback: Answer,
) -> Observation:
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


def _set_team_router_settings(router_settings: dict[str, Any]) -> None:
    response = httpx.post(
        f"{GATEWAY}/team/update",
        headers=_admin_headers(),
        json={"team_id": LAB_TEAM_ID, "router_settings": router_settings},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def _ask(key: str, overrides: dict[str, Any]) -> Answer:
    response = httpx.post(
        f"{GATEWAY}/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": LAB_ROUTE,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": "hi"}],
            **overrides,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return Answer(
        status_code=response.status_code,
        headers={name.lower(): value for name, value in response.headers.items()},
        body=response.text,
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
        served = headers.get("x-brownout-route-served", "")
        group = headers.get("x-litellm-model-group", "")
        if served != group:
            observed.disclosure_mismatches.append((served, group))
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


@requires_stack
def test_every_served_route_header_names_the_group_litellm_served(
    drained: Observation,
) -> None:
    assert drained.disclosure_mismatches == []


@requires_stack
def test_a_team_router_fallback_never_moves_a_governed_request(
    team_fallback: Answer,
) -> None:
    assert team_fallback.status_code != 200, (
        "the failing route answered, so the team's router fallback served the request"
    )
    assert "x-brownout-route-served" not in team_fallback.headers
    assert int(team_fallback.headers.get("x-litellm-attempted-fallbacks", "0")) == 0


@requires_stack
def test_caller_sent_context_window_fallbacks_are_refused(
    caller_fallback: Answer,
) -> None:
    assert caller_fallback.status_code == 400
    assert "caller_routing_not_permitted" in caller_fallback.body


@dataclass(frozen=True)
class InitRun:
    exit_code: int
    output: str

    @property
    def findings(self) -> set[tuple[str, str]]:
        found = set()
        for line in self.output.splitlines():
            match = FINDING_LINE.match(line)
            if match is not None and match.group(2) != "...":
                found.add((match.group(1), match.group(2)))
        return found

    def resolution(self, rule: str, subject: str) -> list[str]:
        lines = self.output.splitlines()
        start = lines.index(
            next(line for line in lines if line.startswith(f"{rule}\t{subject}\t"))
        )
        resolution = []
        for line in lines[start + 1 :]:
            if not line.startswith("    "):
                break
            resolution.append(line.strip())
        return resolution


@pytest.fixture(scope="module")
def init_key(lab_run: LabRun) -> Iterator[str]:
    response = httpx.post(
        f"{GATEWAY}/user/new",
        headers=_admin_headers(),
        json={"user_role": "proxy_admin_viewer"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    viewer = response.json()
    yield viewer["key"]
    _delete_keys([viewer["key"]])
    httpx.post(
        f"{GATEWAY}/user/delete",
        headers=_admin_headers(),
        json={"user_ids": [viewer["user_id"]]},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def _run_init(arguments: list[str], key: str) -> InitRun:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name != "LITELLM_MASTER_KEY"
    }
    environment["BROWNOUT_INIT_KEY"] = key
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "brownout.cli",
            "init",
            "--gateway",
            GATEWAY,
            *arguments,
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
        check=False,
    )
    return InitRun(result.returncode, result.stdout)


def _run_decision(command: str) -> int:
    result = subprocess.run(
        ["bash", "-c", f"{command} -s -o /dev/null -w '%{{http_code}}'"],
        capture_output=True,
        text=True,
        check=False,
    )
    return int(result.stdout or 0)


def _generate_key(body: dict[str, Any]) -> str:
    response = httpx.post(
        f"{GATEWAY}/key/generate",
        headers=_admin_headers(),
        json=body,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()["key"]


def _delete_keys(keys: list[str]) -> None:
    httpx.post(
        f"{GATEWAY}/key/delete",
        headers=_admin_headers(),
        json={"keys": keys},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


@requires_stack
def test_init_from_nothing_prepares_a_gateway_and_its_commands_resolve_the_findings(
    lab_run: LabRun, init_key: str, tmp_path: Path
) -> None:
    baseline = _run_init(["--gateway-config", str(LAB_CONFIG)], init_key)
    suffix = datetime.now(UTC).strftime("%H%M%S%f")
    teamless_alias = f"e2e-teamless-{suffix}"
    capped_alias = f"e2e-capped-{suffix}"
    teamless_key = _generate_key({"key_alias": teamless_alias})
    capped_key = _generate_key(
        {"key_alias": capped_alias, "team_id": LAB_TEAM_ID, "max_budget": 5.0}
    )
    try:
        manifest_out = tmp_path / "brownout.yaml"
        config_out = tmp_path / "brownout.config.yaml"
        prepared = _run_init(
            [
                "--gateway-config",
                str(LAB_CONFIG),
                "--manifest-out",
                str(manifest_out),
                "--config-out",
                str(config_out),
                "--write",
            ],
            init_key,
        )

        assert prepared.exit_code == 1
        assert hash_token(init_key) not in {subject for _, subject in prepared.findings}
        assert ("team-in-manifest", teamless_alias) in prepared.findings
        assert ("key-has-no-budget", capped_alias) in prepared.findings
        written = Manifest.from_file(manifest_out).team_policy(LAB_TEAM_ID)
        assert written is not None
        assert (written.envelope_usd, written.budget_duration) == (
            LAB_ENVELOPE_USD,
            "7d",
        )
        checked = subprocess.run(
            [
                sys.executable,
                "-m",
                "brownout.cli",
                "check",
                "--gateway-config",
                str(config_out),
                "--manifest",
                str(manifest_out),
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        assert checked.returncode == 0, checked.stdout

        for rule, subject, filled in [
            ("team-in-manifest", teamless_alias, LAB_TEAM_ID),
            ("key-has-no-budget", capped_alias, None),
        ]:
            command = prepared.resolution(rule, subject)[0]
            if filled is not None:
                command = command.replace("<YOUR TEAM>", json.dumps(filled))
            assert _run_decision(command) == 200, command

        resolved = _run_init(["--gateway-config", str(LAB_CONFIG)], init_key)
        assert resolved.findings == baseline.findings
        assert resolved.exit_code == baseline.exit_code
    finally:
        _delete_keys([teamless_key, capped_key])


@requires_stack
def test_init_merges_into_an_existing_manifest_and_reports_every_mismatch(
    lab_run: LabRun, init_key: str, tmp_path: Path
) -> None:
    theirs = tmp_path / "theirs.yaml"
    original = LAB_MANIFEST.read_text(encoding="utf-8")
    drifted = original.replace(
        'policy_epoch: "budget-lab-v1"\n    envelope_usd: 1.0\n    budget_duration: "7d"',
        'policy_epoch: "budget-lab-v1"\n    envelope_usd: 2.0\n    budget_duration: "1d"',
    )
    assert drifted != original
    theirs.write_text(drifted, encoding="utf-8")
    manifest_out = tmp_path / "brownout.yaml"

    merged = _run_init(
        [
            "--gateway-config",
            str(LAB_CONFIG),
            "--manifest",
            str(theirs),
            "--manifest-out",
            str(manifest_out),
            "--config-out",
            str(tmp_path / "brownout.config.yaml"),
            "--write",
        ],
        init_key,
    )

    assert merged.exit_code == 1
    assert manifest_out.read_text(encoding="utf-8").startswith(drifted)
    assert {
        ("ceiling-matches-envelope", LAB_TEAM_ID),
        ("duration-matches", LAB_TEAM_ID),
        ("team-exists", "team-a"),
        ("team-exists", "team-b"),
    } <= merged.findings
