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
    if seconds <= 0:
        return
    logger.info("Waiting %.0fs for the batched spend writer to drain", seconds)
    time.sleep(seconds)


def _lab_routes(team: TeamPolicy) -> list[str]:
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
