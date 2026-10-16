from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
import pytest

from .lab import GATEWAY, REQUEST_TIMEOUT_SECONDS, admin_headers, requires_stack
from .support import FIELD_KEYS, REPO_ROOT

KINDS_TEAM_ID: Final = "kinds-lab"
PRIMARY: Final = "budget-lab-primary"
FALLBACK: Final = "budget-lab-degraded"
TOOLS_PRIMARY: Final = "tools-primary"
TOOLS_FALLBACK: Final = "tools-fallback"
EMBEDDING: Final = "lab-embedding"
UNPRICED_EMBEDDING: Final = "lab-embedding-unpriced"
MCP_SERVER: Final = "lab_tools"
LAB_TOOLS: Final = os.getenv("LAB_TOOLS_URL", "http://localhost:18080")
FALLBACK_TEXT: Final = "MOCK budget-lab-degraded"
MAX_TOKENS: Final = 40
MAX_DRIVE_REQUESTS: Final = 60
SPEND_LOG_WAIT_SECONDS: Final = 30.0
POLL_INTERVAL_SECONDS: Final = 1.0
CHAT_MESSAGES: Final = [{"role": "user", "content": "hi"}]
STREAMED_TEXT_KEYS: Final = frozenset({"content", "text"})

pytestmark = pytest.mark.e2e


@dataclass(frozen=True)
class Endpoint:
    path: str
    body: dict[str, Any]
    stream: bool = False


@dataclass(frozen=True)
class Answer:
    status_code: int
    headers: dict[str, str]
    body: str
    json: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        if not self.body.lstrip().startswith(("data:", "event:")):
            return self.body
        pieces: list[str] = []
        for line in self.body.splitlines():
            payload = line.removeprefix("data:").strip()
            if line.startswith("data:") and payload.startswith("{"):
                pieces.extend(_texts(json.loads(payload)))
        return "".join(pieces)


TEXT_ENDPOINTS: Final = {
    "chat": Endpoint(
        "/v1/chat/completions",
        {"model": PRIMARY, "max_tokens": MAX_TOKENS, "messages": CHAT_MESSAGES},
    ),
    "chat-streamed": Endpoint(
        "/v1/chat/completions",
        {
            "model": PRIMARY,
            "max_tokens": MAX_TOKENS,
            "messages": CHAT_MESSAGES,
            "stream": True,
        },
        stream=True,
    ),
    "text-completion": Endpoint(
        "/v1/completions",
        {"model": PRIMARY, "max_tokens": MAX_TOKENS, "prompt": "hi"},
    ),
    "messages": Endpoint(
        "/v1/messages",
        {"model": PRIMARY, "max_tokens": MAX_TOKENS, "messages": CHAT_MESSAGES},
    ),
    "messages-streamed": Endpoint(
        "/v1/messages",
        {
            "model": PRIMARY,
            "max_tokens": MAX_TOKENS,
            "messages": CHAT_MESSAGES,
            "stream": True,
        },
        stream=True,
    ),
    "responses": Endpoint(
        "/v1/responses",
        {"model": PRIMARY, "max_output_tokens": MAX_TOKENS, "input": "hi"},
    ),
    "generate-content": Endpoint(
        f"/v1beta/models/{PRIMARY}:generateContent",
        {
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": MAX_TOKENS},
        },
    ),
}


@pytest.fixture(scope="module")
def kinds_key() -> str:
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tests" / "setup_lab.py"),
            "--team-id",
            KINDS_TEAM_ID,
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"could not prepare the kinds lab team: {result.stderr.strip()}")
    _update_team({"object_permission": {"mcp_servers": [MCP_SERVER]}})
    return result.stdout.strip().splitlines()[-1]


@pytest.fixture(scope="module")
def past_tier(kinds_key: str) -> str:
    chat = TEXT_ENDPOINTS["chat"]
    for _ in range(MAX_DRIVE_REQUESTS):
        answer = _post(kinds_key, chat)
        assert answer.status_code == 200, answer.body
        if "x-brownout-degraded-from" in answer.headers:
            return kinds_key
    pytest.fail(f"the kinds lab never degraded in {MAX_DRIVE_REQUESTS} requests")


@pytest.fixture
def capped_key(kinds_key: str) -> Iterator[str]:
    key = _generate_key({"team_id": KINDS_TEAM_ID, "max_budget": 5.0})
    yield key
    httpx.post(
        f"{GATEWAY}/key/delete",
        headers=admin_headers(),
        json={"keys": [key]},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


@requires_stack
@pytest.mark.parametrize("name", sorted(TEXT_ENDPOINTS))
def test_every_text_endpoint_is_downgraded_past_the_tier(
    past_tier: str, name: str
) -> None:
    answer = _post(past_tier, TEXT_ENDPOINTS[name])

    assert answer.status_code == 200, answer.body
    assert answer.headers["x-brownout-route-served"] == FALLBACK
    assert answer.headers["x-brownout-degraded-from"] == PRIMARY
    assert FALLBACK_TEXT in answer.text


@requires_stack
def test_a_downgraded_messages_call_carries_its_field_into_the_spend_logs(
    past_tier: str,
) -> None:
    answer = _post(past_tier, TEXT_ENDPOINTS["messages"])

    facts = _field_in_spend_logs(answer.json["id"])

    assert (facts["kind"], facts["outcome"]) == ("text", "degraded")
    assert facts["served_route"] == FALLBACK
    assert set(facts) <= FIELD_KEYS


@requires_stack
def test_embeddings_are_served_by_the_model_named_past_the_tier(
    past_tier: str,
) -> None:
    answer = _post(
        past_tier, Endpoint("/v1/embeddings", {"model": EMBEDDING, "input": "hi"})
    )

    assert answer.status_code == 200, answer.body
    assert answer.headers["x-brownout-route-served"] == EMBEDDING
    assert "x-brownout-degraded-from" not in answer.headers


@requires_stack
def test_an_unpriced_embedding_model_is_refused(kinds_key: str) -> None:
    models = _team_models()
    _update_team({"models": [*models, UNPRICED_EMBEDDING]})
    try:
        answer = _post(
            kinds_key,
            Endpoint("/v1/embeddings", {"model": UNPRICED_EMBEDDING, "input": "hi"}),
        )
    finally:
        _update_team({"models": models})

    assert answer.status_code == 503
    assert "unpriced_model" in answer.body


@requires_stack
def test_a_governed_team_calls_an_mcp_tool(past_tier: str) -> None:
    answer = _call_echo(past_tier)

    assert answer.status_code == 200, answer.body
    assert "echoed:hi" in answer.body


@requires_stack
def test_an_mcp_tool_call_from_a_key_with_its_own_budget_is_refused(
    capped_key: str,
) -> None:
    answer = _call_echo(capped_key)

    assert answer.status_code == 503
    assert "key_has_own_budget" in answer.body


@requires_stack
def test_a_tool_loop_is_answered_by_the_fallback_on_every_turn(
    past_tier: str,
) -> None:
    marker = f"tool-loop-{uuid.uuid4().hex}"
    answer = _post(
        past_tier,
        Endpoint(
            "/v1/chat/completions",
            {
                "model": TOOLS_PRIMARY,
                "max_tokens": MAX_TOKENS,
                "messages": [{"role": "user", "content": marker}],
                "tools": [
                    {
                        "type": "mcp",
                        "server_label": MCP_SERVER,
                        "server_url": f"litellm_proxy/mcp/{MCP_SERVER}",
                        "require_approval": "never",
                    }
                ],
            },
        ),
    )

    assert answer.status_code == 200, answer.body
    assert answer.headers["x-brownout-route-served"] == TOOLS_FALLBACK
    assert f"answered by {TOOLS_FALLBACK}" in answer.body
    assert _models_seen(marker) == [TOOLS_FALLBACK, TOOLS_FALLBACK]


@requires_stack
def test_compaction_is_served_by_the_model_named_past_the_tier(
    past_tier: str,
) -> None:
    marker = f"compact-{uuid.uuid4().hex}"
    answer = _post(
        past_tier,
        Endpoint("/v1/responses/compact", {"model": TOOLS_PRIMARY, "input": marker}),
    )

    assert answer.status_code == 200, answer.body
    assert answer.headers["x-brownout-route-served"] == TOOLS_PRIMARY
    assert "x-brownout-degraded-from" not in answer.headers
    assert _models_seen(marker) == [TOOLS_PRIMARY]


@requires_stack
def test_a_stored_response_is_read_back(past_tier: str) -> None:
    marker = f"stored-{uuid.uuid4().hex}"
    created = _post(
        past_tier,
        Endpoint(
            "/v1/responses",
            {"model": TOOLS_PRIMARY, "max_output_tokens": MAX_TOKENS, "input": marker},
        ),
    )
    assert created.status_code == 200, created.body

    read = httpx.get(
        f"{GATEWAY}/v1/responses/{created.json['id']}",
        headers={"Authorization": f"Bearer {past_tier}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    assert read.status_code == 200, read.text
    assert f"answered by {TOOLS_FALLBACK}" in read.text
    assert _models_seen(marker) == [TOOLS_FALLBACK]


@requires_stack
def test_a_governed_team_lists_its_files(kinds_key: str) -> None:
    listed = httpx.get(
        f"{GATEWAY}/v1/files",
        params={"target_model_names": TOOLS_PRIMARY},
        headers={"Authorization": f"Bearer {kinds_key}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    assert listed.status_code == 200, listed.text
    assert listed.json()["object"] == "list"


@requires_stack
def test_a_call_type_outside_the_allow_list_is_refused(kinds_key: str) -> None:
    answer = _post(
        kinds_key,
        Endpoint(
            "/v1/batches",
            {
                "input_file_id": "file-lab",
                "endpoint": "/v1/chat/completions",
                "completion_window": "24h",
            },
        ),
    )

    assert answer.status_code == 503
    assert "call_type_not_governed" in answer.body
    assert "acreate_batch" in answer.body


def _post(key: str, endpoint: Endpoint) -> Answer:
    headers: Final = {"Authorization": f"Bearer {key}"}
    url: Final = f"{GATEWAY}{endpoint.path}"
    if endpoint.stream:
        with httpx.stream(
            "POST",
            url,
            headers=headers,
            json=endpoint.body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            body = "".join(response.iter_text())
            return Answer(response.status_code, _lowered(response.headers), body)
    posted: Final = httpx.post(
        url, headers=headers, json=endpoint.body, timeout=REQUEST_TIMEOUT_SECONDS
    )
    parsed: dict[str, Any] = {}
    if posted.headers.get("content-type", "").startswith("application/json"):
        parsed = posted.json()
    return Answer(posted.status_code, _lowered(posted.headers), posted.text, parsed)


def _call_echo(key: str) -> Answer:
    listed = httpx.get(
        f"{GATEWAY}/mcp-rest/tools/list",
        headers={"Authorization": f"Bearer {key}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    listed.raise_for_status()
    server_id = next(
        tool["mcp_info"]["server_id"]
        for tool in listed.json()["tools"]
        if tool["mcp_info"]["server_name"] == MCP_SERVER
    )
    return _post(
        key,
        Endpoint(
            "/mcp-rest/tools/call",
            {"server_id": server_id, "name": "echo", "arguments": {"text": "hi"}},
        ),
    )


def _models_seen(marker: str) -> list[str]:
    response = httpx.get(
        f"{LAB_TOOLS}/requests",
        params={"prompt": marker},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return list(response.json())


def _field_in_spend_logs(request_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + SPEND_LOG_WAIT_SECONDS
    while time.monotonic() < deadline:
        response = httpx.get(
            f"{GATEWAY}/spend/logs",
            params={"request_id": request_id},
            headers=admin_headers(),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        for record in response.json():
            spend_logs_metadata = (record.get("metadata") or {}).get(
                "spend_logs_metadata"
            ) or {}
            if "brownout" in spend_logs_metadata:
                return spend_logs_metadata["brownout"]
        time.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(f"no spend log carried the brownout field for {request_id}")


def _update_team(fields: dict[str, Any]) -> None:
    response = httpx.post(
        f"{GATEWAY}/team/update",
        headers=admin_headers(),
        json={"team_id": KINDS_TEAM_ID, **fields},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


def _team_models() -> list[str]:
    response = httpx.get(
        f"{GATEWAY}/team/info",
        params={"team_id": KINDS_TEAM_ID},
        headers=admin_headers(),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return list(response.json()["team_info"]["models"])


def _generate_key(body: dict[str, Any]) -> str:
    response = httpx.post(
        f"{GATEWAY}/key/generate",
        headers=admin_headers(),
        json=body,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return str(response.json()["key"])


def _texts(value: Any) -> list[str]:
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in value.items():
            if key in STREAMED_TEXT_KEYS and isinstance(item, str):
                found.append(item)
            else:
                found.extend(_texts(item))
        return found
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    return []


def _lowered(headers: httpx.Headers) -> dict[str, str]:
    return {name.lower(): value for name, value in headers.items()}
