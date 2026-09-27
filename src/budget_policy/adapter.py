"""The LiteLLM seam.

Loads as a dotted callback path in ``config.yaml`` against the stock
``berriai/litellm`` image. It translates the authenticated request into the pure
policy input, applies the outcome to the request body, and discloses what it did
to the caller.

Three things it deliberately does not do, because LiteLLM 1.101.0 already does
them: reserve the worst-case cost, count spend atomically across replicas, and
raise the 429 at 100% of the owner-approved team budget. The reservation lands
during authentication, so the counter this hook reads already holds a charge for
a request that has not run. The ceiling is owed that charge and a tier is not,
so the hook removes this request's own hold before measuring a tier and leaves
every other in-flight hold in place.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC
from enum import StrEnum
from typing import Any, Final, Literal, assert_never

from fastapi import HTTPException, status
from litellm.caching.dual_cache import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.duration_parser import duration_in_seconds
from litellm.proxy._types import ProxyException, UserAPIKeyAuth
from litellm.proxy.auth.auth_checks import (
    _can_object_call_model,
    _resolve_key_models_for_auth_check,
)
from litellm.proxy.common_utils.timezone_utils import get_budget_reset_time
from litellm.proxy.utils import _check_and_merge_model_level_guardrails

from .catalog import CatalogError, FallbackRejected, PriceCatalog, UnpricedModel
from .manifest import Manifest, RoutePolicy, TeamPolicy
from .metrics import DatadogMetricSink
from .policy import Action, Decision, PolicyRequest, Reason, decide
from .runtime import router, spend_reader
from .signals import EventEmitter, IncidentEvent, TierEvent, TierSignalStore

logger: Final = logging.getLogger("budget_policy")

CHAT_COMPLETION_CALL_TYPES: Final = frozenset({"completion", "acompletion"})
DISCLOSURE_KEY: Final = "_budget_policy"
RESERVED_METADATA_PREFIX: Final = "x-platform-"
CALLER_ROUTING_KEYS: Final = ("fallbacks", "mock_testing_fallbacks")
METADATA_CONTAINERS: Final = ("metadata", "litellm_metadata")
_CEILING_TOLERANCE_USD: Final = 1e-9

UNGOVERNED_WINDOW_DURATION: Final = "7d"
"""Only ever used to date and expire the incident for a team with no manifest entry.

Such a team has no declared window, so the dedup key borrows the one every
declared team uses. It bounds how often the incident repeats and nothing else."""

_ALREADY_SIGNALLED_IN_THE_GATEWAY: Final[frozenset[int]] = frozenset()
"""Empty on purpose.

Inside the gateway the atomic store is the only thing that decides whether a
tier has already been signalled, because it is the only thing that sees every
replica. Caching its answer here would create a second authority that outlives
the store whenever the store loses a key, and would then suppress an event the
store would have granted. So the decision reports every tier currently crossed
and the claim in Redis does the deduplication."""

HEADER_ROUTE_SERVED: Final = "x-platform-route-served"
HEADER_DEGRADED_FROM: Final = "x-platform-degraded-from"
HEADER_DEGRADATION_REASON: Final = "x-platform-degradation-reason"
HEADER_BUDGET_TIER: Final = "x-platform-budget-tier"
HEADER_DEPLOYMENT_ID: Final = "x-platform-deployment-id"

DISCLOSED_REASONS: Final = frozenset(
    {Reason.DEGRADE_TIER_REACHED.value, Reason.FALLBACK_REJECTED.value}
)
"""The two reasons a caller is owed.

One says the served model changed. The other says it was supposed to and could
not, which is the case a team would otherwise have to read a platform log to
discover."""


class Entitlement(StrEnum):
    """Whether the caller may be served a degradation target.

    ``UNAVAILABLE`` is not a denial. It is the entitlement check itself failing,
    which is a platform fault rather than a policy outcome. Both refuse the
    target, and separating them stops a router regression reading as an ordinary
    narrow allowlist."""

    PERMITTED = "permitted"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


def _team_counter_key(team_id: str) -> str:
    """The counter LiteLLM keeps a team's spend in.

    Named once because the reader and the hold both address the same key, and a
    hold matched against the wrong key would be silently ignored.
    """
    return f"spend:team:{team_id}"


class TeamSpendReader:
    """Reads the team spend counter LiteLLM already maintains.

    A seam, so the policy can be tested without a gateway and so an unreadable
    counter is a distinguishable outcome rather than a silent zero.
    """

    async def read(
        self, team_id: str, fallback_spend: float, max_budget: float
    ) -> float:
        return float(
            await spend_reader()(
                counter_key=_team_counter_key(team_id),
                fallback_spend=fallback_spend,
                max_budget=max_budget,
            )
        )


class BudgetPolicyHandler(CustomLogger):
    """Tier events, route-declared degradation, and caller disclosure."""

    def __init__(
        self,
        catalog: PriceCatalog | None = None,
        manifest: Manifest | None = None,
        store: TierSignalStore | None = None,
        emitter: EventEmitter | None = None,
        spend_reader: TeamSpendReader | None = None,
    ) -> None:
        super().__init__()
        if catalog is None:
            catalog = PriceCatalog.from_file(_prices_path())
        if manifest is None:
            manifest = Manifest.from_file(_manifest_path())
        if store is None:
            store = TierSignalStore()
        if emitter is None:
            emitter = EventEmitter(_webhook_url(), _metric_sink())
        if spend_reader is None:
            spend_reader = TeamSpendReader()

        self._catalog: Final = catalog
        self._manifest: Final = manifest
        self._store: Final = store
        self._emitter: Final = emitter
        self._spend_reader: Final = spend_reader
        logger.info(
            "budget_policy loaded: catalog=%s manifest=%s teams=%s",
            self._catalog.version,
            self._manifest.version,
            ",".join(self._manifest.team_ids),
        )

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: str,
    ) -> dict:
        if call_type not in CHAT_COMPLETION_CALL_TYPES:
            return data
        _clear_disclosure(data)
        _refuse_caller_routing(data)

        team = self._manifest.team_policy(user_api_key_dict.team_id)
        if team is None:
            await self._report_ungoverned_team(user_api_key_dict.team_id)
            raise _ungoverned_team(user_api_key_dict.team_id)

        route = str(data.get("model") or "")
        route_policy = team.route_policy(route)
        window_id = _window_id(team.budget_duration)

        await self._require_priced(route=route, team=team, window_id=window_id)
        await self._require_budget_ceiling(
            auth=user_api_key_dict, team=team, window_id=window_id
        )

        decision = decide(
            PolicyRequest(
                team=team,
                route=route_policy,
                projected_spend_usd=await self._projected_spend(
                    user_api_key_dict, team
                ),
                already_signalled=_ALREADY_SIGNALLED_IN_THE_GATEWAY,
                selectable_fallback=await self._selectable_fallback(
                    user_api_key_dict, route, route_policy, team, window_id
                ),
            )
        )

        if decision.action is Action.BLOCK:
            await self._publish(
                team=team, window_id=window_id, route=route, decision=decision
            )
            raise _enforcement_unavailable(team=team, route=route)

        applied: Final = _apply(data, decision, team)
        await self._publish(
            team=team, window_id=window_id, route=route, decision=decision
        )
        return applied

    async def async_post_call_response_headers_hook(
        self,
        data: dict,
        user_api_key_dict: UserAPIKeyAuth,
        response: Any,
        request_headers: dict[str, str] | None = None,
        litellm_call_info: dict[str, Any] | None = None,
    ) -> dict[str, str] | None:
        """Disclose the decision on the success path.

        LiteLLM wraps this hook in a try/except that swallows, and it runs on
        success only, so disclosure is a strong default rather than an
        invariant. A request blocked at the native budget ceiling carries no
        platform header and its signal lives in the 429 body.
        """
        disclosure = (data.get("metadata") or {}).get(DISCLOSURE_KEY)
        if not isinstance(disclosure, dict):
            return None

        headers = {HEADER_ROUTE_SERVED: str(disclosure["served_route"])}
        deployment_id = (litellm_call_info or {}).get("model_id")
        if deployment_id is not None:
            headers[HEADER_DEPLOYMENT_ID] = str(deployment_id)
        if disclosure.get("tier") is not None:
            headers[HEADER_BUDGET_TIER] = str(disclosure["tier"])
        if disclosure.get("degraded_from") is not None:
            headers[HEADER_DEGRADED_FROM] = str(disclosure["degraded_from"])
        if str(disclosure["reason"]) in DISCLOSED_REASONS:
            headers[HEADER_DEGRADATION_REASON] = str(disclosure["reason"])
        return headers

    async def _require_priced(
        self, route: str, team: TeamPolicy, window_id: str
    ) -> None:
        """Refuse an unpriced model rather than admit it.

        Reservation runs at authentication, before any callback, so this cannot
        stop LiteLLM from skipping the reservation for a model it could not
        price. What it can do is stop that request reaching a provider.

        The incident is deduplicated on the model rather than the team, because
        the missing price is a fact about the catalog and every team calling
        that route hits the same gap. The team it names is therefore the first
        one observed, not the full set affected.
        """
        try:
            self._catalog.price(route)
        except UnpricedModel:
            await self._incident(
                reason="unpriced_model",
                window_id=window_id,
                detail={
                    "model": route,
                    "team_id": team.team_id,
                    "catalog_version": self._catalog.version,
                },
                dedup=f"incident:unpriced_model:{route}:{window_id}",
                ttl_seconds=_window_ttl(team.budget_duration),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "budget_policy_unavailable",
                    "reason": "unpriced_model",
                    "model": route,
                    "message": (
                        f"Model {route!r} has no committed price in catalog version "
                        f"{self._catalog.version}, so its spend cannot be bounded. The "
                        "request was refused rather than admitted at an unknown cost."
                    ),
                },
            ) from None

    async def _report_ungoverned_team(self, team_id: str | None) -> None:
        """Page the platform for a team the manifest does not declare.

        Such a key has no trustworthy envelope or price policy, so it is
        refused. Letting it pass would recreate the unattributed-spend failure
        this component exists to close.
        """
        window_id: Final = _window_id(UNGOVERNED_WINDOW_DURATION)
        incident_team_id = team_id
        if incident_team_id is None:
            incident_team_id = "<missing>"
        await self._incident(
            reason="team_not_in_manifest",
            window_id=window_id,
            detail={
                "team_id": incident_team_id,
                "manifest_version": self._manifest.version,
            },
            dedup=f"incident:team_not_in_manifest:{incident_team_id}:{window_id}",
            ttl_seconds=_window_ttl(UNGOVERNED_WINDOW_DURATION),
        )

    async def _require_budget_ceiling(
        self,
        auth: UserAPIKeyAuth,
        team: TeamPolicy,
        window_id: str,
    ) -> None:
        """Require LiteLLM's hard ceiling to equal the owner-approved budget.

        Every tier is a percentage of ``envelope_usd`` and the native 429 must
        own exactly 100%. Missing or drifted runtime state breaks that contract,
        so the failure direction is closed.
        """
        ceiling = auth.team_max_budget
        if ceiling is not None and (
            abs(float(ceiling) - team.envelope_usd) <= _CEILING_TOLERANCE_USD
        ):
            return
        actual_ceiling = None
        if ceiling is not None:
            actual_ceiling = float(ceiling)
        await self._incident(
            reason="budget_ceiling_drift",
            window_id=window_id,
            detail={
                "team_id": team.team_id,
                "envelope_usd": team.envelope_usd,
                "team_max_budget_usd": actual_ceiling,
                "manifest_version": self._manifest.version,
            },
            dedup=f"incident:budget_ceiling_drift:{team.team_id}:{window_id}:{actual_ceiling}",
            ttl_seconds=_window_ttl(team.budget_duration),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "budget_policy_unavailable",
                "reason": "budget_ceiling_drift",
                "team_id": team.team_id,
                "message": (
                    "The runtime team budget no longer equals the owner-approved "
                    "envelope, so no tier below it means what it claims."
                ),
            },
        )

    async def _projected_spend(
        self, auth: UserAPIKeyAuth, team: TeamPolicy
    ) -> float | None:
        """Spend committed by the requests the gateway has already admitted.

        The counter LiteLLM maintains carries this request's own worst-case hold,
        placed during authentication. That hold is exactly what the native
        ceiling at 100% is for. A tier asks a different question, how much of its
        envelope the team has consumed, and this request has consumed nothing at
        the moment it is being judged. Left in, every request would push itself
        toward its own downgrade by the size of its own reservation, so a tier
        declared at 75 would fire at 75 minus that size.

        Holds placed by other in-flight requests stay in the figure. Those
        requests were already admitted and each will produce a real charge, so
        dropping them would let a burst cross the tier together unnoticed.

        Returns None when the counter cannot be read, which hands the decision
        to the route's declared posture instead of to an implicit admit.
        """
        try:
            counter = await self._spend_reader.read(
                team_id=team.team_id,
                fallback_spend=float(auth.team_spend or 0.0),
                max_budget=(
                    float(auth.team_max_budget)
                    if auth.team_max_budget is not None
                    else team.envelope_usd
                ),
            )
        except Exception:
            logger.exception("Team spend counter unreadable for team=%s", team.team_id)
            return None
        return max(0.0, counter - _own_hold_on_team_counter(auth, team.team_id))

    async def _selectable_fallback(
        self,
        auth: UserAPIKeyAuth,
        route: str,
        route_policy: RoutePolicy,
        team: TeamPolicy,
        window_id: str,
    ) -> str | None:
        """The degradation target, or None when it may not serve this request.

        A rejection is debug detail rather than a warning per request, because
        the tier event carries ``fallback_rejected`` once per team per window
        and that is the bounded record. An entitlement check that could not run
        is the exception: it is a platform fault and gets an incident.
        """
        if route_policy.fallback is None:
            return None
        try:
            selected = self._catalog.select_fallback(
                primary=route, candidate=route_policy.fallback
            )
        except CatalogError:
            logger.exception("Fallback validation failed for route=%s", route)
            return None
        if isinstance(selected, FallbackRejected):
            logger.debug(
                "Rejected degradation target %s for route %s on team %s: %s",
                selected.candidate,
                route,
                team.team_id,
                selected.reason,
            )
            return None
        assert selected is not None, (
            f"route {route!r} declares a fallback, so selection cannot return no target"
        )

        entitlement = _caller_may_call(auth, selected)
        match entitlement:
            case Entitlement.PERMITTED:
                return selected
            case Entitlement.DENIED:
                logger.debug(
                    "Rejected degradation target %s for route %s on team %s: outside the caller allowlist",
                    selected,
                    route,
                    team.team_id,
                )
                return None
            case Entitlement.UNAVAILABLE:
                await self._incident(
                    reason="entitlement_check_unavailable",
                    window_id=window_id,
                    detail={
                        "team_id": team.team_id,
                        "route": route,
                        "target": selected,
                    },
                    dedup=f"incident:entitlement_check_unavailable:{team.team_id}:{window_id}",
                    ttl_seconds=_window_ttl(team.budget_duration),
                )
                return None
            case _:
                assert_never(entitlement)

    async def _publish(
        self,
        team: TeamPolicy,
        window_id: str,
        route: str,
        decision: Decision,
    ) -> None:
        """Report the decision to whoever owns it.

        A decision taken without a counter is a platform incident and carries no
        tier, because there is no spend figure to have crossed one.
        """
        if decision.reason is Reason.COUNTER_UNAVAILABLE:
            await self._report_unreadable_counter(
                team=team, window_id=window_id, route=route, decision=decision
            )
            return
        await self._emit_tiers_crossed(
            team=team, window_id=window_id, route=route, decision=decision
        )

    async def _report_unreadable_counter(
        self, team: TeamPolicy, window_id: str, route: str, decision: Decision
    ) -> None:
        await self._incident(
            reason="spend_counter_unreadable",
            window_id=window_id,
            detail={
                "team_id": team.team_id,
                "route": route,
                "posture": decision.action.value,
            },
            dedup=f"incident:spend_counter_unreadable:{team.team_id}:{window_id}",
            ttl_seconds=_window_ttl(team.budget_duration),
        )

    async def _emit_tiers_crossed(
        self, team: TeamPolicy, window_id: str, route: str, decision: Decision
    ) -> None:
        """One event per tier, once per team per window across every replica.

        The claim is what deduplicates. A replica that loses the race skips the
        tier rather than emitting a duplicate, and the epoch in the key retires
        every claim when the team's policy changes.

        The events go out together. One request can cross several tiers at once,
        and delivering them one after another would add a remote deadline per
        tier to a single inference request. Gathering holds the added latency at
        one round of ``WEBHOOK_TIMEOUT_SECONDS`` and ``SUBMIT_TIMEOUT_SECONDS``
        however many tiers were crossed.
        """
        assert decision.utilisation is not None
        ttl_seconds: Final = _window_ttl(team.budget_duration)
        claimed: list[TierEvent] = []
        for tier in decision.tiers_crossed:
            claim_key = f"tier:{team.team_id}:{team.policy_epoch}:{window_id}:{tier}"
            if not await self._store.claim(claim_key, ttl_seconds):
                continue
            claimed.append(
                TierEvent(
                    team_id=team.team_id,
                    policy_epoch=team.policy_epoch,
                    window=window_id,
                    tier=tier,
                    route=route,
                    served_route=decision.served_route,
                    decision=decision.action.value,
                    reason=decision.reason.value,
                    spend_usd=decision.utilisation * team.envelope_usd,
                    envelope_usd=team.envelope_usd,
                )
            )
        await asyncio.gather(*(self._emitter.emit_tier(event) for event in claimed))

    async def _incident(
        self,
        reason: str,
        window_id: str,
        detail: dict[str, Any],
        dedup: str,
        ttl_seconds: int,
    ) -> None:
        if not await self._store.claim(dedup, ttl_seconds):
            return
        await self._emitter.emit_incident(
            IncidentEvent(reason=reason, window=window_id, detail=detail)
        )


def _apply(data: dict, decision: Decision, team: TeamPolicy) -> dict:
    """Rewrite the route when degrading, then record the disclosure.

    LiteLLM merges model-level guardrails into the request just before the
    pre-call hook, keyed on the model that was requested. Rewriting the model
    here would leave the target's guardrails unapplied, so the merge is re-run
    against the new model. The merge is a set union, so re-running it is
    idempotent and can only add controls.
    """
    if decision.action is Action.DEGRADE:
        data["model"] = decision.served_route
        data = _remerge_guardrails(data)

    metadata = data.setdefault("metadata", {})
    metadata[DISCLOSURE_KEY] = {
        "served_route": decision.served_route,
        "degraded_from": decision.degraded_from,
        "tier": decision.tier,
        "reason": decision.reason.value,
        "team_id": team.team_id,
    }
    return data


def _own_hold_on_team_counter(auth: UserAPIKeyAuth, team_id: str) -> float:
    """How much of the team counter this request is holding for itself.

    ``reserve_budget_for_request`` returns one entry per counter it touched, and
    only the team entry belongs to the figure a team tier is measured against.
    It returns nothing at all when the worst case cannot be estimated, which is
    the unpriced-route case, and the counter then carries no hold to remove.

    An entry holds ``reserved_cost`` plus the adjustment already pushed onto the
    counter beyond it. Summing the two is what keeps the amount right after
    LiteLLM resizes a reservation down to the budget that remains.
    """
    reservation: Final = auth.budget_reservation
    if reservation is None:
        return 0.0
    counter_key: Final = _team_counter_key(team_id)
    return sum(
        float(entry.get("reserved_cost") or 0.0)
        + float(entry.get("applied_adjustment") or 0.0)
        for entry in reservation.get("entries") or []
        if isinstance(entry, dict) and entry.get("counter_key") == counter_key
    )


def _caller_may_call(auth: UserAPIKeyAuth, model: str) -> Entitlement:
    """Ask LiteLLM whether this caller is entitled to the degradation target.

    The model allowlist is enforced during authentication against the model the
    caller named, and a rewrite after that point is never re-checked, so without
    this a downgrade could serve a model the caller is not permitted to use.
    Reusing LiteLLM's own check keeps one authority for entitlement rather than
    a second copy of the rule that can drift from it.

    Both scopes have to permit the target. A key may be narrower than its team,
    and checking only the team would let a rewrite serve a model that a direct
    request to it would have refused with a 403.

    Each scope consults its direct model list, so a target reachable only
    through an access group reads as not selectable and the primary is served
    instead. That costs a downgrade rather than granting access.

    A check that could not run outranks a denial, because the two need different
    responses. Both refuse the target and only one is a platform fault.
    """
    scopes: Final = (
        _scope_may_call(
            auth,
            model,
            models=_resolve_key_models_for_auth_check(auth),
            object_type="key",
        ),
        _scope_may_call(
            auth, model, models=list(auth.team_models or []), object_type="team"
        ),
    )
    if Entitlement.UNAVAILABLE in scopes:
        return Entitlement.UNAVAILABLE
    if Entitlement.DENIED in scopes:
        return Entitlement.DENIED
    return Entitlement.PERMITTED


def _scope_may_call(
    auth: UserAPIKeyAuth,
    model: str,
    models: list[str],
    object_type: Literal["key", "team"],
) -> Entitlement:
    """One scope's answer, with a denial kept apart from a broken check.

    ``_can_object_call_model`` raises ``ProxyException`` to say no. Anything
    else it raises means the check did not reach an answer, which a bare
    ``except`` would have turned into an ordinary no and hidden.
    """
    try:
        _can_object_call_model(
            model=model,
            llm_router=router(),
            models=models,
            team_id=auth.team_id,
            object_type=object_type,
        )
    except ProxyException:
        return Entitlement.DENIED
    except Exception:
        logger.exception(
            "Entitlement check for model %s in %s scope did not complete",
            model,
            object_type,
        )
        return Entitlement.UNAVAILABLE
    return Entitlement.PERMITTED


def _remerge_guardrails(data: dict) -> dict:
    return _check_and_merge_model_level_guardrails(
        data=data,
        llm_router=router(),
        trust_client_model_info=False,
    )


def _clear_disclosure(data: dict) -> None:
    """Drop any inbound copy of this component's own disclosure key.

    Removed rather than refused, because ``_apply`` rewrites it authoritatively
    later on the same request. A forged value can never be read by anything, so
    refusing it would cost a caller a 400 for something that cannot matter.
    """
    for container in METADATA_CONTAINERS:
        metadata = data.get(container)
        if isinstance(metadata, dict):
            metadata.pop(DISCLOSURE_KEY, None)


def _refuse_caller_routing(data: dict) -> None:
    """Refuse a request that tries to steer or forge a platform decision.

    Stripping these silently was the worse failure. A caller using per-request
    ``fallbacks`` saw them stop working with nothing to grep for, which is the
    shape of problem this component exists to make visible. Refusing names the
    keys, so a developer learns on the first request instead of debugging
    silence.

    Only caller-supplied routing is refused. Fallbacks declared in
    ``router_settings`` live on the Router rather than in the request body
    (``router.py:953``), so nothing LiteLLM offers natively is affected.
    """
    refused: Final = _caller_routing_keys(data)
    if not refused:
        return
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "error": "caller_routing_not_permitted",
            "reason": "caller_routing_not_permitted",
            "refused": refused,
            "message": (
                "This route is governed by a budget policy that selects and validates its own "
                "degradation target. Caller-supplied routing and reserved platform metadata are "
                "refused rather than ignored, so a request is never served as though it had been "
                "honoured when it was not."
            ),
        },
    )


def _caller_routing_keys(data: dict) -> list[str]:
    """Every caller-supplied key that would interfere with the decision."""
    found: list[str] = [key for key in CALLER_ROUTING_KEYS if key in data]
    for container in METADATA_CONTAINERS:
        metadata = data.get(container)
        if not isinstance(metadata, dict):
            continue
        for key in metadata:
            if _is_reserved_metadata_key(str(key)):
                found.append(f"{container}.{key}")
    return sorted(found)


def _is_reserved_metadata_key(key: str) -> bool:
    if key.lower().startswith(RESERVED_METADATA_PREFIX):
        return True
    return key == "degraded_from"


def _enforcement_unavailable(team: TeamPolicy, route: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "budget_policy_unavailable",
            "reason": "spend_counter_unreadable",
            "team_id": team.team_id,
            "message": (
                f"The spend counter for team {team.team_id!r} could not be read, and "
                f"route {route!r} declares a fail-closed posture. Retry shortly."
            ),
        },
    )


def _ungoverned_team(team_id: str | None) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "budget_policy_unavailable",
            "reason": "team_not_in_manifest",
            "team_id": team_id,
            "message": (
                "This key's team declares no budget envelope or price policy, so its "
                "spend cannot be attributed or bounded."
            ),
        },
    )


def _window_id(budget_duration: str) -> str:
    """The identity of the current budget window.

    Asked of LiteLLM with the gateway's own ``timezone`` and ``budget_reset_time``,
    the settings its reset job stamps ``budget_reset_at`` with, so a reset changes
    this value and re-arms every tier without a TTL guess. Rendered in UTC so a
    claim key keeps one form whatever timezone the gateway runs in.
    """
    return get_budget_reset_time(budget_duration).astimezone(UTC).isoformat()


def _window_ttl(budget_duration: str) -> int:
    return duration_in_seconds(budget_duration) * 2


def _prices_path() -> str:
    return os.getenv("BUDGET_POLICY_PRICES", "config/prices.yaml")


def _manifest_path() -> str:
    return os.getenv("BUDGET_POLICY_MANIFEST", "config/manifest.yaml")


def _webhook_url() -> str | None:
    return os.getenv("BUDGET_POLICY_WEBHOOK_URL")


def _metric_sink() -> DatadogMetricSink | None:
    """Only when Datadog is actually configured, so a lab without it stays quiet."""
    api_key = os.getenv("DD_API_KEY")
    if not api_key:
        return None
    return DatadogMetricSink(
        api_key=api_key,
        site=os.getenv("DD_SITE", "datadoghq.com"),
        env=os.getenv("DD_ENV", "unknown"),
    )


handler: Final = BudgetPolicyHandler()
