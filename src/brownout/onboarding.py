from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Any, Final, Literal, assert_never

from .gateway_api import LiteLLMKey, LiteLLMModel, LiteLLMTeam
from .manifest import WINDOW_SECONDS, Manifest, TeamPolicy
from .refusals import RuleId, Violation

HANDLER_PATH: Final = "brownout.gateway.handler"
DEFAULT_WARN_TIERS: Final = (75, 90)
EVERY_MODEL: Final = "all-proxy-models"
BUDGET_ALERTS: Final = "budget_alerts"
REFUSAL_STATUS: Final = 429
CEILING_TOLERANCE_USD: Final = 1e-9
REDIS_TARGETS: Final = ("host", "url", "startup_nodes", "sentinel_nodes")
REDIS_CONNECTION_FIELDS: Final = (
    "host",
    "port",
    "url",
    "username",
    "password",
    "ssl",
    "db",
    "startup_nodes",
    "sentinel_nodes",
    "service_name",
    "sentinel_password",
)
BUDGET_PLACEHOLDER: Final = "<YOUR BUDGET>"
DURATION_PLACEHOLDER: Final = "<YOUR DURATION, SUCH AS 7d>"
TEAM_PLACEHOLDER: Final = "<YOUR TEAM>"

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
            "policy_epoch": f"{self.team_id}-v1",
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
    return [_team_finding(team, gateway) for team in teams if not _governable(team)]


def key_findings(keys: Iterable[LiteLLMKey], gateway: str) -> list[Finding]:
    findings: list[Finding] = []
    for key in keys:
        subject = key.alias or key.token
        if key.team_id is None:
            findings.append(
                Finding(
                    RuleId.TEAM_IN_MANIFEST,
                    subject,
                    "has no team, so its chat calls will be refused",
                    (
                        _post(
                            gateway,
                            "/key/update",
                            [("key", _json(key.token)), ("team_id", TEAM_PLACEHOLDER)],
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
                    f"carries its own {', '.join(key.own_budgets)}, so its chat calls "
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


def model_findings(models: Iterable[LiteLLMModel]) -> list[Finding]:
    return [
        Finding(
            RuleId.MODEL_PRICED,
            model.name,
            "has no input and output price, so requests for it will be refused",
            (
                f"- model_name: {model.name}",
                "  litellm_params:",
                "    input_cost_per_token: <YOUR INPUT PRICE PER TOKEN>",
                "    output_cost_per_token: <YOUR OUTPUT PRICE PER TOKEN>",
            ),
            True,
        )
        for model in models
        if not model.priced
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


def config_plan(
    config: Mapping[str, Any], default_alert_types: Sequence[str]
) -> tuple[ConfigChange, ...]:
    general: Final = _section(config, "general_settings")
    litellm_settings: Final = _section(config, "litellm_settings")
    changes: list[ConfigChange] = []
    callbacks = litellm_settings.get("callbacks")
    if not isinstance(callbacks, list) or HANDLER_PATH not in callbacks:
        changes.append(AppendSetting("litellm_settings", "callbacks", HANDLER_PATH))
    if general.get("fail_closed_budget_enforcement") is not True:
        changes.append(
            SetSetting(
                "general_settings",
                "fail_closed_budget_enforcement",
                True,
                "sets the budget boundary for every budget on the gateway, governed or not",
            )
        )
    if "disable_budget_reservation" in general:
        changes.append(RemoveSetting("general_settings", "disable_budget_reservation"))
    changes.extend(_alert_types(general, default_alert_types))
    if "budget_exceeded_status_code" not in litellm_settings:
        changes.append(
            SetSetting(
                "litellm_settings",
                "budget_exceeded_status_code",
                REFUSAL_STATUS,
                "recommended. Keeps LiteLLM's budget refusal a 429 from 1.104",
            )
        )
    changes.extend(_coordination_redis(general, litellm_settings))
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


def master_key_notice() -> Finding:
    return Finding(
        RuleId.TEAM_IN_MANIFEST,
        "master key",
        "has no team, so chat calls made with it will be refused. Send chat traffic "
        "with team keys",
        (),
        False,
    )


def nothing_to_govern() -> Finding:
    return Finding(
        RuleId.TEAM_IN_MANIFEST,
        "manifest",
        "no LiteLLM team has a positive max_budget and an accepted budget_duration, "
        "so there is no manifest to write. Resolve the team findings first",
        (),
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
        fields.append(("budget_duration", DURATION_PLACEHOLDER))
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
        "accept, so its chat calls will be refused",
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
        f"{team.max_budget}, so its chat calls will be refused",
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


def _alert_types(
    general: Mapping[str, Any], default_alert_types: Sequence[str]
) -> list[ConfigChange]:
    if not general.get("alerting"):
        return []
    current = general.get("alert_types")
    if current is None:
        current = list(default_alert_types)
    if BUDGET_ALERTS not in current:
        return []
    return [
        SetSetting(
            "general_settings",
            "alert_types",
            [alert for alert in current if alert != BUDGET_ALERTS],
            "drops budget_alerts and keeps LiteLLM's other alerts",
        )
    ]


def _coordination_redis(
    general: Mapping[str, Any], litellm_settings: Mapping[str, Any]
) -> list[ConfigChange]:
    if general.get("coordination_redis"):
        return []
    cache_params = litellm_settings.get("cache_params")
    if not isinstance(cache_params, Mapping) or cache_params.get("type") != "redis":
        return []
    if not any(cache_params.get(target) for target in REDIS_TARGETS):
        return []
    return [
        SetSetting(
            "general_settings",
            "coordination_redis",
            {
                field: cache_params[field]
                for field in REDIS_CONNECTION_FIELDS
                if field in cache_params
            },
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


def _section(config: Mapping[str, Any], name: Section) -> Mapping[str, Any]:
    section = config.get(name)
    if not isinstance(section, Mapping):
        return {}
    return section


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
