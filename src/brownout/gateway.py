from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final, assert_never

from litellm.caching.dual_cache import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth

from . import bridge, settings, transitions
from .events import EventLog
from .manifest import Manifest, RoutePolicy, TeamPolicy
from .policy import (
    Facts,
    Permission,
    Rejected,
    Rejection,
    WantsDegrade,
    decide,
    resolve,
)
from .ports import (
    Caller,
    ClaimStore,
    EventSink,
    GatewayState,
    Guardrails,
    Permissions,
    ResetSchedule,
    SpendCounter,
)
from .refusals import Refusal, RefusalReason, RuleId
from .rules import GroupLookup, ModelGroup, is_priced, route_rules, settings_rules

logger: Final = logging.getLogger("brownout")

UNGOVERNED_WINDOW_DURATION: Final = "7d"
CEILING_TOLERANCE_USD: Final = 1e-9


@dataclass(frozen=True)
class Checked:
    team: TeamPolicy
    route: RoutePolicy
    spend_usd: float
    window: str


@dataclass(frozen=True)
class Refused:
    refusal: Refusal
    window: str
    ttl_seconds: int


class BrownoutHandler(CustomLogger):
    def __init__(
        self,
        manifest: Manifest,
        spend_counter: SpendCounter,
        permissions: Permissions,
        schedule: ResetSchedule,
        guardrails: Guardrails,
        gateway: GatewayState,
        claims: ClaimStore,
        events: EventSink,
    ) -> None:
        super().__init__()
        self._manifest: Final = manifest
        self._spend_counter: Final = spend_counter
        self._permissions: Final = permissions
        self._schedule: Final = schedule
        self._guardrails: Final = guardrails
        self._gateway: Final = gateway
        self._claims: Final = claims
        self._events: Final = events

    @classmethod
    def from_environment(cls) -> BrownoutHandler:
        bridge.require_supported_litellm(bridge.running_litellm())
        manifest: Final = Manifest.from_file(settings.manifest_path())
        handler = cls(
            manifest=manifest,
            spend_counter=bridge.LiteLLMSpendCounter(),
            permissions=bridge.LiteLLMPermissions(bridge.live_router),
            schedule=bridge.LiteLLMResetSchedule(),
            guardrails=bridge.LiteLLMGuardrails(bridge.live_router),
            gateway=bridge.LiteLLMGatewayState(
                bridge.live_router, bridge.live_general_settings
            ),
            claims=bridge.RedisClaimStore(bridge.live_spend_counter_redis),
            events=EventLog(),
        )
        logger.info(
            "brownout loaded: manifest=%s teams=%s",
            manifest.version,
            ",".join(manifest.team_ids),
        )
        return handler

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: str,
    ) -> dict:
        bridge.clear_field(data)
        if call_type not in bridge.CHAT_COMPLETION_CALL_TYPES:
            return data
        caller: Final = bridge.parse_caller(user_api_key_dict)
        checked: Final = await self._check(caller, data)
        match checked:
            case Refused(refusal=refusal, window=window, ttl_seconds=ttl_seconds):
                await self._report(refusal, window, ttl_seconds)
                bridge.write_field(data, bridge.refused_field(data, refusal))
                raise bridge.http_error(refusal)
            case Checked(team=team, route=route, spend_usd=spend_usd, window=window):
                return await self._govern(caller, data, team, route, spend_usd, window)
            case _:
                assert_never(checked)

    async def async_post_call_response_headers_hook(
        self,
        data: dict,
        user_api_key_dict: UserAPIKeyAuth,
        response: Any,
        request_headers: dict[str, str] | None = None,
        litellm_call_info: dict[str, Any] | None = None,
    ) -> dict[str, str] | None:
        return bridge.headers(data, response, litellm_call_info)

    async def _govern(
        self,
        caller: Caller,
        data: dict,
        team: TeamPolicy,
        route: RoutePolicy,
        spend_usd: float,
        window: str,
    ) -> dict:
        decision: Final = decide(Facts(team=team, route=route, spend_usd=spend_usd))
        permission: Permission | None = None
        if isinstance(decision.wants, WantsDegrade):
            permission = self._permissions.check(caller, decision.wants.target)
        outcome: Final = resolve(decision, permission)
        applied: Final = bridge.apply(data, outcome, self._guardrails)
        published = transitions.request_transitions(team, window, decision, outcome)
        if (
            isinstance(outcome, Rejected)
            and outcome.rejection is Rejection.ENTITLEMENT_CHECK_UNAVAILABLE
        ):
            published.append(
                transitions.incident(
                    "entitlement_check_unavailable",
                    team.team_id,
                    window,
                    {
                        "team_id": team.team_id,
                        "route": outcome.source,
                        "target": outcome.target,
                    },
                )
            )
        await transitions.publish(
            self._claims,
            self._events,
            transitions.window_ttl(team.budget_duration),
            published,
        )
        bridge.write_field(applied, bridge.governed_field(team, decision, outcome))
        return applied

    async def _check(self, caller: Caller, data: dict) -> Checked | Refused:
        refused_keys: Final = bridge.caller_routing_keys(data)
        if refused_keys:
            return self._refused(
                _caller_routing(refused_keys), UNGOVERNED_WINDOW_DURATION
            )

        team: Final = self._manifest.team_policy(caller.team_id)
        if team is None:
            return self._refused(
                _ungoverned(caller.team_id), UNGOVERNED_WINDOW_DURATION
            )

        model: Final = bridge.requested_model(data)
        route: Final = team.route_policy(model)
        groups: Final = _memoised(self._gateway.model_group)
        checks: Final = (
            self._key_without_budget(caller, team),
            self._priced(model, team, groups),
            _ceiling_matches(caller, team),
            self._gateway_rules(team, route, groups),
        )
        for refusal in checks:
            if refusal is not None:
                return self._refused(refusal, team.budget_duration)

        try:
            counter = await self._spend_counter.read(
                team_id=team.team_id,
                fallback_spend=caller.team_spend,
                max_budget=team.envelope_usd,
            )
        except Exception:
            logger.exception("Team spend counter unreadable for team=%s", team.team_id)
            return self._refused(_unreadable(team, model), team.budget_duration)

        return Checked(
            team=team,
            route=route,
            spend_usd=max(0.0, counter - caller.own_hold_usd),
            window=transitions.window_id(self._schedule, team.budget_duration),
        )

    def _refused(self, refusal: Refusal, budget_duration: str) -> Refused:
        return Refused(
            refusal=refusal,
            window=transitions.window_id(self._schedule, budget_duration),
            ttl_seconds=transitions.window_ttl(budget_duration),
        )

    def _key_without_budget(self, caller: Caller, team: TeamPolicy) -> Refusal | None:
        if not caller.key_budgets:
            return None
        return Refusal(
            rule=RuleId.KEY_HAS_NO_BUDGET,
            reason=RefusalReason.KEY_HAS_OWN_BUDGET,
            message=(
                "This key carries a budget of its own. Brownout governs team budgets, "
                "and a key budget is a second ceiling its tiers know nothing about."
            ),
            detail={
                "team_id": team.team_id,
                "key_budgets": list(caller.key_budgets),
            },
            incident_subject=team.team_id,
        )

    def _priced(
        self, model: str, team: TeamPolicy, groups: GroupLookup
    ) -> Refusal | None:
        group = groups(model)
        if group is not None and is_priced(group):
            return None
        served = self._gateway.serves(model, team.team_id)
        if served:
            subject = f"group:{model}"
        else:
            subject = f"team:{team.team_id}"
        return Refusal(
            rule=RuleId.MODEL_PRICED,
            reason=RefusalReason.UNPRICED_MODEL,
            message=(
                f"Model {model!r} has no input and output price, so its spend cannot be "
                "bounded. The request was refused rather than admitted at an unknown cost."
            ),
            detail={
                "model": model,
                "model_group_served": served,
                "team_id": team.team_id,
            },
            incident_subject=subject,
        )

    def _gateway_rules(
        self, team: TeamPolicy, route: RoutePolicy, groups: GroupLookup
    ) -> Refusal | None:
        violations = settings_rules(self._gateway.settings()) + route_rules(
            self._manifest, route, groups
        )
        if not violations:
            return None
        first = violations[0]
        return Refusal(
            rule=first.rule,
            reason=RefusalReason.GATEWAY_RULE_VIOLATED,
            message=f"{first.subject} {first.message}",
            detail={"team_id": team.team_id, "subject": first.subject},
            incident_subject=f"{first.rule.value}:{first.subject}",
        )

    async def _report(self, refusal: Refusal, window: str, ttl_seconds: int) -> None:
        if refusal.incident_subject is None:
            return
        await transitions.publish(
            self._claims,
            self._events,
            ttl_seconds,
            [
                transitions.incident(
                    refusal.reason.value,
                    refusal.incident_subject,
                    window,
                    {**refusal.detail, "rule": refusal.rule.value},
                )
            ],
        )


def _memoised(lookup: GroupLookup) -> GroupLookup:
    found: dict[str, ModelGroup | None] = {}

    def cached(name: str) -> ModelGroup | None:
        if name not in found:
            found[name] = lookup(name)
        return found[name]

    return cached


def _caller_routing(refused_keys: list[str]) -> Refusal:
    return Refusal(
        rule=RuleId.NO_CALLER_ROUTING,
        reason=RefusalReason.CALLER_ROUTING_NOT_PERMITTED,
        message=(
            "This route is governed by brownout, which selects its own degradation "
            "target. Caller-supplied routing and reserved brownout metadata are refused "
            "rather than ignored."
        ),
        detail={"refused": refused_keys},
        incident_subject=None,
    )


def _ungoverned(team_id: str | None) -> Refusal:
    subject = team_id
    if subject is None:
        subject = "<missing>"
    return Refusal(
        rule=RuleId.TEAM_IN_MANIFEST,
        reason=RefusalReason.TEAM_NOT_IN_MANIFEST,
        message=(
            "This key's team declares no budget in the brownout manifest, so its spend "
            "cannot be governed."
        ),
        detail={"team_id": subject},
        incident_subject=subject,
    )


def _ceiling_matches(caller: Caller, team: TeamPolicy) -> Refusal | None:
    ceiling: Final = caller.team_max_budget
    if (
        ceiling is not None
        and abs(ceiling - team.envelope_usd) <= CEILING_TOLERANCE_USD
    ):
        return None
    return Refusal(
        rule=RuleId.CEILING_MATCHES_ENVELOPE,
        reason=RefusalReason.BUDGET_CEILING_DRIFT,
        message=(
            "The team's LiteLLM max_budget no longer equals the manifest's envelope_usd, "
            "so no tier below it means what it claims."
        ),
        detail={
            "team_id": team.team_id,
            "envelope_usd": team.envelope_usd,
            "team_max_budget_usd": ceiling,
        },
        incident_subject=f"{team.team_id}:{ceiling}",
    )


def _unreadable(team: TeamPolicy, model: str) -> Refusal:
    return Refusal(
        rule=RuleId.SPEND_READABLE,
        reason=RefusalReason.SPEND_COUNTER_UNREADABLE,
        message=f"The spend counter for team {team.team_id!r} could not be read. Retry shortly.",
        detail={"team_id": team.team_id, "route": model},
        incident_subject=team.team_id,
    )


handler: Final = BrownoutHandler.from_environment()
