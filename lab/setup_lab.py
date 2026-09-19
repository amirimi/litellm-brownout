"""Create the lab team, envelope and key the end-to-end smoke test drives.

Reads the budget straight out of `config/manifest.yaml`, so the LiteLLM team
`max_budget` and the owner-approved policy budget are equal by construction.
The native 429 owns 100% and this component owns every configurable tier below
it.

Rerunnable. It tears the team down, waits out LiteLLM's batched spend writer,
clears the Redis spend counter and the tier claims for the window, then
rebuilds, so a second demo run starts from zero rather than from wherever the
first one stopped.

The wait is load-bearing. LiteLLM buffers spend per team and flushes it every
`PROXY_BATCH_WRITE_AT` seconds (`constants.py:1693`, 10 by default). Deleting the
team and recreating it under the same id inside that window lets the previous
run's buffered spend land on the new row, and the next clean Redis miss reseeds
the counter straight off it. Deleting first and creating after the flush window
leaves that write with no row to hit.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Any, Final

import httpx
import redis

from budget_policy.manifest import Manifest, TeamPolicy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger: Final = logging.getLogger("setup_lab")

LAB_TEAM_ID: Final = "budget-lab"
REQUEST_TIMEOUT_SECONDS: Final = 30.0
SPEND_FLUSH_SETTLE_SECONDS: Final = 11.0


def main() -> int:
    args = _parse_args()
    manifest = Manifest.from_file(args.manifest)
    team = manifest.team_policy(args.team_id)
    if team is None:
        logger.error("Team %s is not declared in %s", args.team_id, args.manifest)
        return 1

    with httpx.Client(
        base_url=args.gateway,
        headers={"Authorization": f"Bearer {args.master_key}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    ) as client:
        existed = _delete_team(client, team.team_id)
        if existed:
            _await_spend_flush(args.settle_seconds)
        _clear_counters(args.redis_url, team.team_id)
        _create_team(client, team, _lab_routes(team))
        key = _create_key(client, team)

    logger.info(
        "Lab ready: team=%s envelope=$%.2f window=%s",
        team.team_id,
        team.envelope_usd,
        team.budget_duration,
    )
    print(key)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway", default=os.getenv("LITELLM_GATEWAY", "http://localhost:4000")
    )
    parser.add_argument("--master-key", default=os.getenv("LITELLM_MASTER_KEY", ""))
    parser.add_argument(
        "--manifest",
        default=os.getenv("BUDGET_POLICY_MANIFEST", "config/manifest.yaml"),
    )
    parser.add_argument("--team-id", default=LAB_TEAM_ID)
    parser.add_argument("--redis-url", default=os.getenv("LAB_REDIS_URL", ""))
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=float(
            os.getenv("LAB_SPEND_FLUSH_SETTLE_SECONDS", SPEND_FLUSH_SETTLE_SECONDS)
        ),
    )
    return parser.parse_args()


def _await_spend_flush(seconds: float) -> None:
    """Let the previous run's buffered spend flush before the team id is reused.

    Only reached when a team was actually removed, so a first run pays nothing.
    """
    if seconds <= 0:
        return
    logger.info("Waiting %.0fs for the batched spend writer to drain", seconds)
    time.sleep(seconds)


def _lab_routes(team: TeamPolicy) -> list[str]:
    """Every route the team may reach, primaries and their degradation targets.

    The team's model allowlist has to include the fallback. A degradation the
    team is not entitled to serve would be rejected by LiteLLM after this
    component selected it.
    """
    routes: set[str] = set(team.routes)
    for policy in team.routes.values():
        if policy.fallback is not None:
            routes.add(policy.fallback)
    return sorted(routes)


def _delete_team(client: httpx.Client, team_id: str) -> bool:
    response = client.post("/team/delete", json={"team_ids": [team_id]})
    if response.status_code < 300:
        logger.info("Removed the previous %s team", team_id)
        return True
    logger.info("No previous %s team to remove (%s)", team_id, response.status_code)
    return False


def _create_team(client: httpx.Client, team: TeamPolicy, models: list[str]) -> None:
    payload: dict[str, Any] = {
        "team_id": team.team_id,
        "team_alias": team.team_id,
        "max_budget": team.envelope_usd,
        "budget_duration": team.budget_duration,
        "models": models,
    }
    response = client.post("/team/new", json=payload)
    _raise_for_status(response, f"create team {team.team_id}")
    logger.info(
        "Created team %s with max_budget=$%.2f over %s",
        team.team_id,
        team.envelope_usd,
        team.budget_duration,
    )


def _create_key(client: httpx.Client, team: TeamPolicy) -> str:
    """Mint a team key with no key-level budget.

    A key budget would become a second ceiling and the team envelope would stop
    being the only thing this component enforces against.
    """
    response = client.post(
        "/key/generate",
        json={
            "team_id": team.team_id,
            "key_alias": f"{team.team_id}-smoke-{os.getpid()}",
            "max_budget": None,
            "models": [],
        },
    )
    _raise_for_status(response, f"mint key for {team.team_id}")
    return response.json()["key"]


def _clear_counters(redis_url: str, team_id: str) -> None:
    """Zero the spend counter and the tier claims, or fail.

    This used to log and carry on. A wrong Redis password then produced a script
    that reported a ready lab while the counter still held the previous run's
    spend, and the Postgres team row read zero the whole time because the row
    was genuinely new. Nothing downstream could tell the difference, so the
    failure has to stop here.

    Both patterns are qualified by the team. The claim sweep used to be
    ``*budget_policy:*``, which on a shared development Redis would re-arm every
    other team's tier events as a side effect of resetting this one.
    """
    url: Final = redis_url or _redis_url_from_environment()
    client = redis.from_url(url)
    try:
        removed = _delete_matching(
            client, f"*spend:team:{team_id}*"
        ) + _delete_matching(client, f"*budget_policy:*{team_id}*")
        logger.info("Cleared %d lab counter and claim keys from Redis", removed)
        _verify_counter_cleared(client, team_id)
    finally:
        client.close()


def _verify_counter_cleared(client: Any, team_id: str) -> None:
    """Read the counter back rather than trusting the delete."""
    survivors: Final = [
        key.decode() for key in client.scan_iter(match=f"*spend:team:{team_id}*")
    ]
    if survivors:
        raise RuntimeError(
            f"Spend counter keys survived the reset for {team_id}: {sorted(survivors)}"
        )


def _delete_matching(client: Any, pattern: str) -> int:
    keys = list(client.scan_iter(match=pattern, count=500))
    if not keys:
        return 0
    return int(client.delete(*keys))


def _redis_url_from_environment() -> str:
    host = os.getenv("LAB_REDIS_HOST", "localhost")
    port = os.getenv("LAB_REDIS_PORT", "6379")
    password = os.getenv("REDIS_PASSWORD", "")
    if password:
        credentials = f":{password}@"
    else:
        credentials = ""
    return f"redis://{credentials}{host}:{port}/0"


def _raise_for_status(response: httpx.Response, action: str) -> None:
    if response.status_code >= 300:
        raise RuntimeError(
            f"Failed to {action}: {response.status_code} {response.text}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
