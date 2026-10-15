from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Any, Final, Literal, assert_never

from .gateway_api import LiteLLMKey, LiteLLMTeam
from .gateway_config import GatewayConfig
from .manifest import WINDOW_SECONDS, Manifest, TeamPolicy
from .refusals import RuleId, Violation
from .rules import ModelGroup, is_priced

HANDLER_PATH: Final = "brownout.gateway.handler"
DEFAULT_WARN_TIERS: Final = (75, 90)
EVERY_MODEL: Final = "all-proxy-models"
REFUSAL_STATUS: Final = 429
CEILING_TOLERANCE_USD: Final = 1e-9
BUDGET_PLACEHOLDER: Final = "<YOUR BUDGET>"
DURATION_PLACEHOLDER: Final = "<YOUR DURATION, SUCH AS 7d>"
TEAM_PLACEHOLDER: Final = "<YOUR TEAM ID>"

Section = Literal["general_settings", "litellm_settings"]


@dataclass(frozen=True)
class Finding:
    rule: RuleId
    subject: str
    detail: str
    resolution: tuple[str, ...]
    blocking: bool


@dataclass(frozen=True)
class TeamEntry:
    team_id: str
    envelope_usd: float
    budget_duration: str

    def document(self) -> dict[str, Any]:
        return {
            "envelope_usd": self.envelope_usd,
            "budget_duration": self.budget_duration,
            "warn_tiers": list(DEFAULT_WARN_TIERS),
            "routes": {},
        }


@dataclass(frozen=True)
class SetSetting:
    section: Section
    key: str
    value: Any
    note: str | None


@dataclass(frozen=True)
class RemoveSetting:
    section: Section
    key: str


@dataclass(frozen=True)
class AppendSetting:
    section: Section
    key: str
    value: str


ConfigChange = SetSetting | RemoveSetting | AppendSetting


def team_entries(
    teams: Iterable[LiteLLMTeam], existing: frozenset[str]
) -> tuple[TeamEntry, ...]:
    entries = [
        TeamEntry(team.team_id, _budget(team), _duration(team))
        for team in teams
        if _governable(team) and team.team_id not in existing
    ]
    return tuple(sorted(entries, key=lambda entry: entry.team_id))


def team_findings(teams: Iterable[LiteLLMTeam], gateway: str) -> list[Finding]:
    listed: Final = tuple(teams)
    return [
        *(_team_finding(team, gateway) for team in listed if not _governable(team)),
        *(
            _member_finding(team, user_id, gateway)
            for team in listed
            for user_id in team.budgeted_members
        ),
    ]


def _member_finding(team: LiteLLMTeam, user_id: str, gateway: str) -> Finding:
    return Finding(
        RuleId.MEMBER_HAS_NO_BUDGET,
        f"{team.team_id}/{user_id}",
        "carries a budget inside the team, so LiteLLM can refuse its requests before "
        "any tier fires or any route degrades",
        (
            _post(
                gateway,
                "/team/member_update",
                [
                    ("team_id", _json(team.team_id)),
                    ("user_id", _json(user_id)),
                    ("max_budget_in_team", "null"),
                ],
            ),
        ),
        True,
    )


def key_findings(
    keys: Iterable[LiteLLMKey], gateway: str, own_token: str
) -> list[Finding]:
    findings: list[Finding] = []
    for key in keys:
        subject = key.alias or key.token
        if key.team_id is None and key.token != own_token:
            findings.append(
                Finding(
                    RuleId.TEAM_IN_MANIFEST,
                    subject,
                    "has no team, so its model calls will be refused",
                    (
                        _post(
                            gateway,
                            "/key/update",
                            [
                                ("key", _json(key.token)),
                                ("team_id", _json(TEAM_PLACEHOLDER)),
                            ],
                        ),
                    ),
                    True,
                )
            )
        if key.own_budgets:
            findings.append(
                Finding(
                    RuleId.KEY_HAS_NO_BUDGET,
                    subject,
                    f"carries its own {', '.join(key.own_budgets)}, so its model calls "
                    "will be refused",
                    (
                        _post(
                            gateway,
                            "/key/update",
                            [
                                ("key", _json(key.token)),
                                ("max_budget", "null"),
                                ("model_max_budget", "{}"),
                                ("budget_limits", "null"),
                            ],
                        ),
                    ),
                    True,
                )
            )
    return findings


def model_findings(groups: Mapping[str, ModelGroup]) -> list[Finding]:
    return [
        Finding(
            RuleId.MODEL_PRICED,
            name,
            "has no input and output price, so requests for it will be refused. Add "
            "the lines below to its deployments and restart the gateway, because init "
            "reads prices from the running gateway. A model added in the admin UI takes "
            "its price there instead",
            (
                f"- model_name: {name}",
                "  litellm_params:",
                "    input_cost_per_token: <YOUR INPUT PRICE PER TOKEN>",
                "    output_cost_per_token: <YOUR OUTPUT PRICE PER TOKEN>",
            ),
            True,
        )
        for name, group in sorted(groups.items())
        if not is_priced(group)
    ]


def reconcile_findings(
    manifest: Manifest, teams: Iterable[LiteLLMTeam], gateway: str
) -> list[Finding]:
    by_id: Final = {team.team_id: team for team in teams}
    findings: list[Finding] = []
    for policy in manifest.teams:
        team = by_id.get(policy.team_id)
        if team is None:
            findings.append(_missing_team(policy, gateway))
            continue
        findings.extend(_team_mismatches(policy, team, gateway))
    return findings


def config_plan(config: GatewayConfig) -> tuple[ConfigChange, ...]:
    changes: list[ConfigChange] = []
    if HANDLER_PATH not in config.callbacks:
        changes.append(AppendSetting("litellm_settings", "callbacks", HANDLER_PATH))
    if not config.settings.fail_closed_budget_enforcement:
        changes.append(
            SetSetting(
                "general_settings",
                "fail_closed_budget_enforcement",
                True,
                "sets the budget boundary for every budget on the gateway, governed or not",
            )
        )
    if config.settings.disable_budget_reservation:
        changes.append(RemoveSetting("general_settings", "disable_budget_reservation"))
    if not config.budget_exceeded_status_code:
        changes.append(
            SetSetting(
                "litellm_settings",
                "budget_exceeded_status_code",
                REFUSAL_STATUS,
                "recommended. Keeps LiteLLM's budget refusal a 429 from 1.104",
            )
        )
    changes.extend(_coordination_redis(config))
    return tuple(changes)


def violation_findings(violations: Iterable[Violation]) -> list[Finding]:
    return [
        Finding(
            violation.rule,
            violation.subject,
            violation.message,
            _violation_resolution(violation.rule),
            True,
        )
        for violation in violations
    ]


def admin_keys_notice(own_key_teamless: bool) -> Finding:
    if own_key_teamless:
        return Finding(
            RuleId.TEAM_IN_MANIFEST,
            "admin keys",
            "the master key and the key init runs with have no team, so model calls "
            "made with them will be refused. Send model traffic with team keys",
            (),
            False,
        )
    return Finding(
        RuleId.TEAM_IN_MANIFEST,
        "master key",
        "the master key has no team, so model calls made with it will be refused. "
        "Send model traffic with team keys",
        (),
        False,
    )


def nothing_to_govern(gateway: str, teams_exist: bool) -> Finding:
    if teams_exist:
        return Finding(
            RuleId.TEAM_IN_MANIFEST,
            "manifest",
            "no LiteLLM team has a positive max_budget and an accepted budget_duration, "
            "so there is no manifest to write. Resolve the team findings first",
            (),
            True,
        )
    return Finding(
        RuleId.TEAM_IN_MANIFEST,
        "manifest",
        "the gateway has no team, so there is no manifest to write. Create a team with "
        "a budget and a key for it with the commands below, then send model traffic with "
        "that key",
        (
            _post(
                gateway,
                "/team/new",
                [
                    ("team_id", _json(TEAM_PLACEHOLDER)),
                    ("max_budget", BUDGET_PLACEHOLDER),
                    ("budget_duration", _json(DURATION_PLACEHOLDER)),
                ],
            ),
            _post(gateway, "/key/generate", [("team_id", _json(TEAM_PLACEHOLDER))]),
        ),
        True,
    )


def describe_change(change: ConfigChange) -> str:
    match change:
        case AppendSetting(section=section, key=key, value=value):
            return f"add {value} to {section}.{key}"
        case RemoveSetting(section=section, key=key):
            return f"remove {section}.{key}"
        case SetSetting(section=section, key=key, value=value):
            return f"set {section}.{key}{_described_value(value)}"
        case _:
            assert_never(change)


def exit_code(findings: Iterable[Finding]) -> int:
    if any(finding.blocking for finding in findings):
        return 1
    return 0


def _described_value(value: Any) -> str:
    if isinstance(value, bool):
        return f" to {str(value).lower()}"
    if isinstance(value, int):
        return f" to {value}"
    if isinstance(value, Mapping):
        return f" with {', '.join(value)}"
    return ""


def _governable(team: LiteLLMTeam) -> bool:
    return _has_budget(team) and team.budget_duration in WINDOW_SECONDS


def _has_budget(team: LiteLLMTeam) -> bool:
    return (
        team.max_budget is not None
        and isfinite(team.max_budget)
        and team.max_budget > 0
    )


def _budget(team: LiteLLMTeam) -> float:
    assert team.max_budget is not None, f"team {team.team_id!r} has no budget"
    return team.max_budget


def _duration(team: LiteLLMTeam) -> str:
    assert team.budget_duration is not None, f"team {team.team_id!r} has no window"
    return team.budget_duration


def _team_finding(team: LiteLLMTeam, gateway: str) -> Finding:
    fields: list[tuple[str, str]] = [("team_id", _json(team.team_id))]
    if not _has_budget(team):
        fields.append(("max_budget", BUDGET_PLACEHOLDER))
    if team.budget_duration not in WINDOW_SECONDS:
        fields.append(("budget_duration", _json(DURATION_PLACEHOLDER)))
    command: Final = (_post(gateway, "/team/update", fields),)
    if not _has_budget(team):
        return Finding(
            RuleId.TEAM_IN_MANIFEST,
            team.team_id,
            "has no positive max_budget, so it cannot join the manifest and its chat "
            "calls will be refused",
            command,
            True,
        )
    return Finding(
        RuleId.DURATION_TUMBLES,
        team.team_id,
        f"has budget_duration {team.budget_duration!r}, which the manifest does not "
        "accept, so its model calls will be refused",
        command,
        True,
    )


def _missing_team(policy: TeamPolicy, gateway: str) -> Finding:
    return Finding(
        RuleId.TEAM_EXISTS,
        policy.team_id,
        "is in the manifest but not in LiteLLM",
        (
            _post(
                gateway,
                "/team/new",
                [
                    ("team_id", _json(policy.team_id)),
                    ("max_budget", _json(policy.envelope_usd)),
                    ("budget_duration", _json(policy.budget_duration)),
                ],
            ),
            f"or delete teams.{policy.team_id} from the manifest",
        ),
        True,
    )


def _team_mismatches(
    policy: TeamPolicy, team: LiteLLMTeam, gateway: str
) -> list[Finding]:
    findings: list[Finding] = []
    if (
        team.max_budget is None
        or abs(team.max_budget - policy.envelope_usd) > CEILING_TOLERANCE_USD
    ):
        findings.append(_envelope_mismatch(policy, team, gateway))
    if team.budget_duration != policy.budget_duration:
        findings.append(_duration_mismatch(policy, team, gateway))
    findings.extend(_unlisted_fallbacks(policy, team, gateway))
    return findings


def _envelope_mismatch(policy: TeamPolicy, team: LiteLLMTeam, gateway: str) -> Finding:
    in_manifest: str
    if team.max_budget is None:
        in_manifest = f"or delete teams.{policy.team_id} from the manifest"
    else:
        in_manifest = f"or set teams.{policy.team_id}.envelope_usd: {team.max_budget}"
    return Finding(
        RuleId.CEILING_MATCHES_ENVELOPE,
        policy.team_id,
        f"envelope_usd is {policy.envelope_usd} and LiteLLM's max_budget is "
        f"{team.max_budget}, so its model calls will be refused",
        (
            _post(
                gateway,
                "/team/update",
                [
                    ("team_id", _json(policy.team_id)),
                    ("max_budget", _json(policy.envelope_usd)),
                ],
            ),
            in_manifest,
        ),
        True,
    )


def _duration_mismatch(policy: TeamPolicy, team: LiteLLMTeam, gateway: str) -> Finding:
    return Finding(
        RuleId.DURATION_MATCHES,
        policy.team_id,
        f"budget_duration is {policy.budget_duration!r} in the manifest and "
        f"{team.budget_duration!r} in LiteLLM, so tier windows drift from resets",
        (
            _post(
                gateway,
                "/team/update",
                [
                    ("team_id", _json(policy.team_id)),
                    ("budget_duration", _json(policy.budget_duration)),
                ],
            ),
            f"or set teams.{policy.team_id}.budget_duration: {team.budget_duration}",
        ),
        True,
    )


def _unlisted_fallbacks(
    policy: TeamPolicy, team: LiteLLMTeam, gateway: str
) -> list[Finding]:
    if not team.models or EVERY_MODEL in team.models:
        return []
    return [
        Finding(
            RuleId.FALLBACK_IN_TEAM_MODELS,
            f"{policy.team_id}/{route.route}",
            f"falls back to {route.fallback!r}, which the team's LiteLLM model list "
            "omits, so every downgrade will be rejected",
            (
                _post(
                    gateway,
                    "/team/update",
                    [
                        ("team_id", _json(policy.team_id)),
                        ("models", _json([*team.models, route.fallback])),
                    ],
                ),
                (
                    f"or change teams.{policy.team_id}.routes.{route.route}.fallback "
                    "to a model the team may call"
                ),
            ),
            True,
        )
        for route in policy.routes.values()
        if route.fallback is not None and route.fallback not in team.models
    ]


def _coordination_redis(config: GatewayConfig) -> list[ConfigChange]:
    if config.coordination_redis or config.cache_redis is None:
        return []
    return [
        SetSetting(
            "general_settings",
            "coordination_redis",
            dict(config.cache_redis),
            "reuses the Redis that litellm_settings.cache_params names",
        )
    ]


def _violation_resolution(rule: RuleId) -> tuple[str, ...]:
    if rule is not RuleId.REDIS_CONFIGURED:
        return ()
    return (
        "general_settings:",
        "  coordination_redis:",
        "    host: <YOUR REDIS HOST>",
        "    port: 6379",
        "    password: os.environ/REDIS_PASSWORD",
    )


def _json(value: Any) -> str:
    return json.dumps(value)


def _post(gateway: str, path: str, fields: Sequence[tuple[str, str]]) -> str:
    body: Final = (
        "{" + ", ".join(f"{_json(name)}: {value}" for name, value in fields) + "}"
    )
    quoted: Final = body.replace("'", "'\\''")
    return (
        f"curl -X POST {gateway.rstrip('/')}{path} "
        '-H "Authorization: Bearer $LITELLM_MASTER_KEY" '
        '-H "Content-Type: application/json" '
        f"-d '{quoted}'"
    )
