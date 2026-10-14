from __future__ import annotations

import os
import subprocess
from typing import Final

import httpx
import pytest

GATEWAY: Final = os.getenv("LITELLM_GATEWAY", "http://localhost:4000")
REQUEST_TIMEOUT_SECONDS: Final = 30.0
STACK_UNAVAILABLE: Final = (
    "the composed gateway is not reachable, run `docker compose up -d` first"
)


def stack_is_up() -> bool:
    try:
        return httpx.get(f"{GATEWAY}/health/liveliness", timeout=3.0).status_code == 200
    except httpx.HTTPError:
        return False


def stack_is_required() -> bool:
    return os.getenv("LAB_REQUIRE_STACK", "").lower() in {"1", "true", "yes"}


if stack_is_required() and not stack_is_up():
    raise RuntimeError(f"LAB_REQUIRE_STACK is set and {STACK_UNAVAILABLE}")

requires_stack: Final = pytest.mark.skipif(not stack_is_up(), reason=STACK_UNAVAILABLE)


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}"}


def run_printed(command: str) -> int:
    result: Final = subprocess.run(
        ["bash", "-c", f"{command} -s -o /dev/null -w '%{{http_code}}'"],
        capture_output=True,
        text=True,
        check=False,
    )
    return int(result.stdout or 0)
