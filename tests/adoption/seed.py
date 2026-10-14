from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

REQUEST_TIMEOUT_SECONDS: Final = 60.0
TEAM_REFERENCE: Final = "{team:"


@dataclass
class Seeded:
    team_ids: dict[str, str] = field(default_factory=dict)
    keys: dict[str, str] = field(default_factory=dict)

    def resolve(self, value: str) -> str:
        resolved = value
        for alias, team_id in self.team_ids.items():
            resolved = resolved.replace(f"{TEAM_REFERENCE}{alias}}}", team_id)
        if TEAM_REFERENCE in resolved:
            raise KeyError(f"{value} names a team the scenario did not seed")
        return resolved


def seed_gateway(url: str, master_key: str, plan: Mapping[str, Any]) -> Seeded:
    seeded: Final = Seeded()
    with httpx.Client(
        base_url=url,
        headers={"Authorization": f"Bearer {master_key}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    ) as client:
        for model in plan.get("models") or []:
            _post(client, "/model/new", model)
        for team in plan.get("teams") or []:
            body = {name: value for name, value in team.items() if name != "alias"}
            body["team_alias"] = team["alias"]
            seeded.team_ids[team["alias"]] = _post(client, "/team/new", body)["team_id"]
        for user in plan.get("users") or []:
            _post(client, "/user/new", user)
        for member in plan.get("members") or []:
            _post(
                client,
                "/team/member_add",
                {
                    "team_id": seeded.team_ids[member["team"]],
                    "member": {"role": "user", "user_id": member["user_id"]},
                    "max_budget_in_team": member["max_budget_in_team"],
                },
            )
        for key in plan.get("keys") or []:
            body = {
                name: value
                for name, value in key.items()
                if name not in {"alias", "team"}
            }
            body["key_alias"] = key["alias"]
            if "team" in key:
                body["team_id"] = seeded.team_ids[key["team"]]
            seeded.keys[key["alias"]] = _post(client, "/key/generate", body)["key"]
    return seeded


def _post(client: httpx.Client, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
    response: Final = client.post(path, json=dict(body))
    if response.status_code != 200:
        raise RuntimeError(
            f"seeding {path} failed with {response.status_code}: {response.text}"
        )
    payload: Final = response.json()
    assert isinstance(payload, dict)
    return payload
