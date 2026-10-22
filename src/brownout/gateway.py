from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Final, assert_never

from litellm.caching.dual_cache import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth

from . import bridge, log_output, plugin_folder, settings, transitions
from .bridge import CallKind
from .events import EventLog
from .manifest import Manifest, RoutePolicy, TeamPolicy
from .policy import (
    Decision,
    Facts,
    Pairing,
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
from .refusals import Refusal, RefusalReason, RuleId, Violation
from .rules import GroupLookup, ModelGroup, is_priced, route_rules, settings_rules

logger: Final = logging.getLogger("brownout")

UNGOVERNED_WINDOW_DURATION: Final = "7d"
CEILING_TOLERANCE_USD: Final = 1e-9


@dataclass(frozen=True)
class Refused:
    refusal: Refusal
    window: str
    ttl_seconds: int


@dataclass(frozen=True)
class Admitted:
    team: TeamPolicy
    kind: CallKind


@dataclass(frozen=True)
class ModelCall:
    kind: CallKind
    team: TeamPolicy
    route: RoutePolicy
    spend_usd: float
    window: str


@dataclass(frozen=True)
class NoModelCall:
    pass


Checked = ModelCall | NoModelCall | Refused


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
        self._counter_mode_named = False
        self._permissions: Final = permissions
        self._schedule: Final = schedule
        self._guardrails: Final = guardrails
        self._gateway: Final = gateway
        self._claims: Final = claims
        self._events: Final = events

    @classmethod
    def from_environment(cls) -> BrownoutHandler:
        log_output.install(logger, logging.getLogger(log_output.LITELLM_PROXY_LOGGER))
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
            "brownout loaded: version=%s teams=%s",
            plugin_folder.running_version(),
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
        caller: Final = bridge.parse_caller(user_api_key_dict)
        checked: Final = await self._check(caller, data, call_type)
        match checked:
            case Refused(refusal=refusal, window=window, ttl_seconds=ttl_seconds):
                await self._report(refusal, window, ttl_seconds)
                bridge.write_field(data, bridge.refused_field(data, refusal))
                raise bridge.http_error(refusal)
            case ModelCall():
                return await self._govern(caller, data, checked)
            case NoModelCall():
                bridge.write_field(data, bridge.admitted_field())
                return data
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

    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: list,
        messages: list | None,
        request_kwargs: dict | None = None,
        parent_otel_span: Any = None,
    ) -> list:
        routed: Final = request_kwargs or {}
        team: Final = self._manifest.team_policy(bridge.request_team(routed))
        if team is None or not healthy_deployments:
            return healthy_deployments
        refusal: Final = self._verify(model, team, routed, healthy_deployments)
        if refusal is None:
            return healthy_deployments
        await self._report(
            refusal,
            transitions.window_id(self._schedule, team.budget_duration),
            transitions.window_ttl(team.budget_duration),
        )
        raise bridge.http_error(refusal)

    def _verify(
        self,
        model: str,
        team: TeamPolicy,
        routed: dict,
        candidates: list[dict],
    ) -> Refusal | None:
        field: Final = bridge.read_field(routed)
        if field is None:
            return _not_a_group(model, team)
        decided: Final = bridge.decided_route(field)
        if decided is None:
            return None
        if any(candidate.get("model_name") != decided for candidate in candidates):
            return _not_a_group(model, team)
        kind: Final = bridge.field_kind(field)
        if kind is not None and not _priced_per_token(kind):
            return None
        if is_priced(self._gateway.candidate_group(decided, candidates)):
            return None
        return _unpriced(decided, team)

    async def _govern(self, caller: Caller, data: dict, checked: ModelCall) -> dict:
        team: Final = checked.team
        route: Final = checked.route
        window: Final = checked.window
        decision: Final = decide(
            Facts(team=team, route=route, spend_usd=checked.spend_usd)
        )
        broken_pairing: Final = self._broken_pairing(decision, route)
        pairing: Pairing | None = None
        permission: Permission | None = None
        if isinstance(decision.wants, WantsDegrade):
            if broken_pairing:
                pairing = Pairing.BROKEN
            else:
                pairing = Pairing.USABLE
                permission = self._permissions.check(caller, decision.wants.target)
        outcome: Final = resolve(decision, pairing, permission)
        applied: Final = bridge.apply(data, outcome, self._guardrails)
        published = transitions.request_transitions(team, window, decision, outcome)
        published.extend(
            _pairing_incident(team, window, violation)
            for violation in broken_pairing[:1]
        )
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
        bridge.write_field(
            applied, bridge.governed_field(checked.kind, team, decision, outcome)
        )
        return applied

    async def _check(self, caller: Caller, data: dict, call_type: str) -> Checked:
        admitted: Final = self._admit(caller, data, call_type)
        if isinstance(admitted, Refused):
            return admitted
        team: Final = admitted.team
        match admitted.kind:
            case CallKind.NO_MODEL:
                return NoModelCall()
            case (
                CallKind.TEXT
                | CallKind.EMBEDDING
                | CallKind.COMPACTION
                | CallKind.OTHER_MODEL
            ):
                return await self._model_call(caller, data, admitted.kind, team)
            case _:
                assert_never(admitted.kind)

    def _admit(self, caller: Caller, data: dict, call_type: str) -> Admitted | Refused:
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

        kind: Final = bridge.call_kind(call_type)
        if kind is None:
            return self._refused(
                _not_governed_call(call_type, team), team.budget_duration
            )

        checks: Final = (
            self._key_without_budget(caller, team),
            _ceiling_matches(caller, team),
            self._settings_rules(team),
        )
        for refusal in checks:
            if refusal is not None:
                return self._refused(refusal, team.budget_duration)
        return Admitted(team=team, kind=kind)

    def _model_refusal(
        self, model: str, team: TeamPolicy, kind: CallKind
    ) -> Refusal | None:
        concrete: Final = self._concrete(model, team)
        if concrete is not None:
            return concrete
        if not _priced_per_token(kind):
            return None
        return self._priced(model, team, self._gateway.model_group)

    async def _model_call(
        self, caller: Caller, data: dict, kind: CallKind, team: TeamPolicy
    ) -> ModelCall | Refused:
        model: Final = bridge.requested_model(data)
        refusal: Final = self._model_refusal(model, team, kind)
        if refusal is not None:
            return self._refused(refusal, team.budget_duration)
        self._name_counter_mode()
        try:
            counter = await self._spend_counter.read(
                team_id=team.team_id,
                fallback_spend=caller.team_spend,
                max_budget=team.envelope_usd,
            )
        except Exception:
            logger.exception("Team spend counter unreadable for team=%s", team.team_id)
            return self._refused(_unreadable(team, model), team.budget_duration)

        return ModelCall(
            kind=kind,
            team=team,
            route=_route(team, model, kind),
            spend_usd=max(0.0, counter - caller.own_hold_usd),
            window=transitions.window_id(self._schedule, team.budget_duration),
        )

    def _name_counter_mode(self) -> None:
        if self._counter_mode_named:
            return
        self._counter_mode_named = True
        if self._spend_counter.shared():
            logger.info("brownout spend counter: shared through Redis")
            return
        logger.warning(
            "brownout spend counter: per process, because LiteLLM has no Redis. "
            "Brownout supports this on one LiteLLM process only: one replica with "
            "one worker"
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

    def _concrete(self, model: str, team: TeamPolicy) -> Refusal | None:
        if self._gateway.is_concrete(model):
            return None
        return _not_a_group(model, team)

    def _priced(
        self, model: str, team: TeamPolicy, groups: GroupLookup
    ) -> Refusal | None:
        group = groups(model)
        if group is not None and is_priced(group):
            return None
        return _unpriced(model, team)

    def _broken_pairing(
        self, decision: Decision, route: RoutePolicy
    ) -> list[Violation]:
        if not isinstance(decision.wants, WantsDegrade):
            return []
        return route_rules(self._manifest, route, _memoised(self._gateway.model_group))

    def _settings_rules(self, team: TeamPolicy) -> Refusal | None:
        violations = settings_rules(self._gateway.settings())
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


def _priced_per_token(kind: CallKind) -> bool:
    match kind:
        case CallKind.TEXT | CallKind.EMBEDDING | CallKind.COMPACTION:
            return True
        case CallKind.OTHER_MODEL | CallKind.NO_MODEL:
            return False
        case _:
            assert_never(kind)


def _route(team: TeamPolicy, model: str, kind: CallKind) -> RoutePolicy:
    declared: Final = team.route_policy(model)
    match kind:
        case CallKind.TEXT:
            return declared
        case (
            CallKind.EMBEDDING
            | CallKind.COMPACTION
            | CallKind.OTHER_MODEL
            | CallKind.NO_MODEL
        ):
            return replace(declared, fallback=None, degrade_at=None)
        case _:
            assert_never(kind)


def _pairing_incident(
    team: TeamPolicy, window: str, violation: Violation
) -> transitions.Transition:
    return transitions.incident(
        RefusalReason.GATEWAY_RULE_VIOLATED.value,
        f"{violation.rule.value}:{violation.subject}",
        window,
        {
            "team_id": team.team_id,
            "subject": violation.subject,
            "rule": violation.rule.value,
        },
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
            "target. Caller-supplied fallback lists are refused rather than ignored."
        ),
        detail={"refused": refused_keys},
        incident_subject=None,
    )


def _ungoverned(team_id: str | None) -> Refusal:
    if team_id is None:
        return _ungoverned_refusal(
            "<missing>",
            "This key belongs to no team. Brownout governs team budgets only, so its "
            "spend cannot be governed.",
        )
    return _ungoverned_refusal(
        team_id,
        "This key's team declares no budget in the brownout manifest, so its spend "
        "cannot be governed.",
    )


def _ungoverned_refusal(subject: str, message: str) -> Refusal:
    return Refusal(
        rule=RuleId.TEAM_IN_MANIFEST,
        reason=RefusalReason.TEAM_NOT_IN_MANIFEST,
        message=message,
        detail={"team_id": subject},
        incident_subject=subject,
    )


def _not_a_group(model: str, team: TeamPolicy) -> Refusal:
    return Refusal(
        rule=RuleId.MODEL_IS_A_GROUP,
        reason=RefusalReason.MODEL_NOT_A_GROUP,
        message=(
            f"Model {model!r} is not a model group the gateway configures. Brownout "
            "governs requests that name a model group, so it refuses aliases, wildcards, "
            "deployment ids and model lists."
        ),
        detail={"model": model, "team_id": team.team_id},
        incident_subject=f"team:{team.team_id}",
    )


def _unpriced(model: str, team: TeamPolicy) -> Refusal:
    return Refusal(
        rule=RuleId.MODEL_PRICED,
        reason=RefusalReason.UNPRICED_MODEL,
        message=(
            f"Model {model!r} has a deployment without an input and output price, so its "
            "spend cannot be bounded. The request was refused rather than admitted at an "
            "unknown cost."
        ),
        detail={"model": model, "team_id": team.team_id},
        incident_subject=f"group:{model}",
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


def _not_governed_call(call_type: str, team: TeamPolicy) -> Refusal:
    return Refusal(
        rule=RuleId.CALL_TYPE_GOVERNED,
        reason=RefusalReason.CALL_TYPE_NOT_GOVERNED,
        message=(
            f"Brownout admits only the LiteLLM call types it has reviewed, and "
            f"{call_type!r} is not one of them. Use a unified route, such as "
            "/v1/chat/completions, /v1/messages, /v1/responses or /v1/embeddings."
        ),
        detail={"team_id": team.team_id, "call_type": call_type},
        incident_subject=f"{team.team_id}:{call_type}",
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
