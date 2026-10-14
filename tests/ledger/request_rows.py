from __future__ import annotations

from typing import Final

from brownout.refusals import RefusalReason, RuleId

from .model import Form, Input, Open, Refused, Supported

GATEWAY: Final = "tests/test_gateway.py::"
BRIDGE: Final = "tests/test_bridge.py::"
MANIFEST: Final = "tests/test_manifest.py::"
PERMISSIONS: Final = "tests/test_permissions.py::"
EVENTS: Final = "tests/test_events.py::"
SEAMS: Final = "tests/test_litellm_seams.py::"


def _request(rule: RuleId, reason: RefusalReason, *tests: str) -> Refused:
    return Refused(stage="request", rule=rule, tests=tests, reason=reason)


REQUEST_ROWS: Final = (
    Input(
        shape="pre-call hook call type",
        source="brownout",
        parser="gateway.BrownoutHandler.async_pre_call_hook",
        reads=("parameter:call_type", "parameter:data", "parameter:user_api_key_dict"),
        cites=(),
        forms=(
            Form(
                "completion or acompletion",
                "governed",
                Supported(
                    (
                        f"{GATEWAY}test_a_governed_request_discloses_only_facts_about_itself",
                    )
                ),
            ),
            Form(
                "any other call type, such as embeddings, /v1/messages or /v1/responses",
                "passed through at full price",
                Open("non-chat-pass-through"),
            ),
        ),
    ),
    Input(
        shape="the hook contract LiteLLM calls",
        source="brownout",
        parser="gateway.BrownoutHandler",
        reads=("litellm:CustomLogger",),
        cites=(),
        forms=(
            Form(
                "the pre-call and response-header hooks with their parameters",
                "hooks",
                Supported(
                    (
                        f"{SEAMS}test_seam_the_pre_call_and_header_hooks_keep_their_parameters",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="request model",
        source="brownout",
        parser="bridge.requested_model",
        reads=("key:model",),
        cites=("litellm/proxy/common_request_processing.py:1201@1.95.1",),
        forms=(
            Form(
                "a model group the team's manifest routes",
                "governed by its route",
                Supported(
                    (f"{GATEWAY}test_a_route_that_starts_degrading_is_announced",)
                ),
            ),
            Form(
                "a model group with no route in the manifest",
                "served and counted, never degraded",
                Supported(
                    (
                        f"{MANIFEST}test_an_undeclared_route_gets_a_policy_that_never_degrades",
                    )
                ),
            ),
            Form(
                "a name LiteLLM rewrote before the hook, through completion_model, "
                "model_alias_map, key aliases or team aliases",
                "the group LiteLLM resolved",
                Supported(
                    (
                        f"{SEAMS}test_seam_team_and_key_aliases_apply_before_the_pre_call_hook",
                    )
                ),
            ),
            Form(
                "a name LiteLLM maps to a group only after the hook: a router alias, a "
                "wildcard deployment or a team's public model name",
                "served undegraded, or refused as unpriced",
                Open("name-not-a-group"),
            ),
        ),
    ),
    Input(
        shape="caller-sent routing",
        source="brownout",
        parser="bridge.caller_routing_keys",
        reads=(
            "key:fallbacks",
            "key:context_window_fallbacks",
            "key:content_policy_fallbacks",
            "key:mock_testing_fallbacks",
        ),
        cites=(),
        forms=(
            Form(
                "none of the routing keys",
                "governed",
                Supported(
                    (
                        f"{GATEWAY}test_caller_metadata_with_brownout_like_names_is_served",
                    )
                ),
            ),
            Form(
                "any routing key",
                "refused",
                _request(
                    RuleId.NO_CALLER_ROUTING,
                    RefusalReason.CALLER_ROUTING_NOT_PERMITTED,
                    f"{GATEWAY}test_caller_supplied_routing_is_refused_rather_than_ignored",
                    f"{BRIDGE}test_caller_routing_keys_are_named",
                ),
            ),
        ),
    ),
    Input(
        shape="caller-sent brownout field",
        source="brownout",
        parser="bridge.clear_field",
        reads=(
            "key:metadata",
            "key:litellm_metadata",
            "key:spend_logs_metadata",
            "key:brownout",
        ),
        cites=(),
        forms=(
            Form(
                "a brownout field in either metadata container",
                "cleared before the request is judged",
                Supported(
                    (
                        f"{BRIDGE}test_a_forged_field_is_cleared_from_both_containers_without_mutation",
                        f"{GATEWAY}test_a_forged_field_on_a_non_chat_call_is_removed",
                    )
                ),
            ),
            Form(
                "no brownout field",
                "untouched",
                Supported(
                    (
                        f"{BRIDGE}test_clearing_leaves_a_request_without_the_field_untouched",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="request metadata brownout writes into",
        source="brownout",
        parser="bridge.write_field",
        reads=("key:metadata", "key:spend_logs_metadata"),
        cites=(),
        forms=(
            Form(
                "absent or not a mapping",
                "a fresh field",
                Supported(
                    (
                        f"{BRIDGE}test_a_request_without_metadata_gains_the_field",
                        f"{BRIDGE}test_a_spend_logs_metadata_that_is_not_a_dict_is_replaced",
                    )
                ),
            ),
            Form(
                "the key's own spend_logs_metadata",
                "copied, never mutated",
                Supported(
                    (
                        f"{GATEWAY}test_the_keys_own_spend_logs_metadata_is_never_mutated",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="brownout's field in the response hook",
        source="brownout",
        parser="bridge.read_field",
        reads=("key:metadata", "key:spend_logs_metadata", "key:brownout"),
        cites=(),
        forms=(
            Form(
                "the field the pre-call hook wrote",
                "headers",
                Supported(
                    (f"{BRIDGE}test_headers_carry_only_what_the_decision_has_to_say",)
                ),
            ),
            Form(
                "no field, as on a refused request",
                "no headers",
                Supported(
                    (f"{BRIDGE}test_a_refused_request_carries_no_disclosure_headers",)
                ),
            ),
        ),
    ),
    Input(
        shape="brownout's field values",
        source="brownout",
        parser="bridge.headers",
        reads=(
            "key:served_route",
            "key:outcome",
            "key:requested_route",
            "key:budget_tier",
            "key:route_tier",
            "key:degradation_reason",
            "key:model_id",
        ),
        cites=(),
        forms=(
            Form(
                "a served response",
                "route and deployment headers",
                Supported(
                    (f"{BRIDGE}test_headers_carry_only_what_the_decision_has_to_say",)
                ),
            ),
            Form(
                "a failed request",
                "no route-served header",
                Supported(
                    (
                        f"{BRIDGE}test_a_failed_request_never_claims_a_route_served",
                        f"{BRIDGE}test_a_failed_request_with_nothing_to_disclose_carries_no_headers",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="response hook parameters",
        source="brownout",
        parser="gateway.BrownoutHandler.async_post_call_response_headers_hook",
        reads=("parameter:data", "parameter:response", "parameter:litellm_call_info"),
        cites=(),
        forms=(
            Form(
                "the request, its response and LiteLLM's call info",
                "headers",
                Supported(
                    (f"{BRIDGE}test_headers_carry_only_what_the_decision_has_to_say",)
                ),
            ),
        ),
    ),
    Input(
        shape="authenticated caller's team",
        source="brownout",
        parser="bridge.parse_caller",
        reads=(
            "litellm:_resolve_key_models_for_auth_check",
            "member:team_id",
            "member:team_max_budget",
            "member:team_models",
            "member:team_spend",
        ),
        cites=(),
        forms=(
            Form(
                "a team in the manifest whose max_budget equals its envelope",
                "governed",
                Supported((f"{GATEWAY}test_an_aligned_envelope_raises_no_incident",)),
            ),
            Form(
                "no team, or a team outside the manifest",
                "refused",
                _request(
                    RuleId.TEAM_IN_MANIFEST,
                    RefusalReason.TEAM_NOT_IN_MANIFEST,
                    f"{GATEWAY}test_a_key_outside_the_manifest_fails_closed_and_pages_the_platform",
                ),
            ),
            Form(
                "a team max_budget that differs from the envelope, or none",
                "refused",
                _request(
                    RuleId.CEILING_MATCHES_ENVELOPE,
                    RefusalReason.BUDGET_CEILING_DRIFT,
                    f"{GATEWAY}test_an_envelope_that_no_longer_matches_the_team_ceiling_pages_the_platform",
                    f"{GATEWAY}test_a_missing_team_ceiling_fails_closed",
                ),
            ),
            Form(
                "the key's and team's model lists",
                "the allowlists a downgrade is checked against",
                Supported(
                    (
                        f"{BRIDGE}test_a_caller_carries_its_key_budget_and_resolved_models",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="key budgets on the caller",
        source="brownout",
        parser="bridge._key_budgets",
        reads=("member:max_budget", "member:model_max_budget", "member:budget_limits"),
        cites=(),
        forms=(
            Form(
                "none",
                "governed through the team",
                Supported(
                    (
                        f"{BRIDGE}test_a_caller_carries_its_key_budget_and_resolved_models",
                    )
                ),
            ),
            Form(
                "a max_budget, model_max_budget or budget_limits",
                "refused",
                _request(
                    RuleId.KEY_HAS_NO_BUDGET,
                    RefusalReason.KEY_HAS_OWN_BUDGET,
                    f"{GATEWAY}test_a_key_with_a_budget_of_its_own_is_refused",
                ),
            ),
        ),
    ),
    Input(
        shape="the request's own reservation",
        source="brownout",
        parser="bridge._own_hold",
        reads=(
            "member:budget_reservation",
            "member:team_id",
            "key:entries",
            "key:counter_key",
            "key:reserved_cost",
            "key:applied_adjustment",
        ),
        cites=(),
        forms=(
            Form(
                "no reservation",
                "the counter as read",
                Supported(
                    (
                        f"{GATEWAY}test_a_request_with_no_reservation_is_judged_on_the_counter_as_read",
                    )
                ),
            ),
            Form(
                "entries on the team counter",
                "subtracted from the counter",
                Supported(
                    (
                        f"{GATEWAY}test_a_request_is_judged_without_its_own_reservation",
                        f"{BRIDGE}test_only_the_team_counter_hold_is_removed",
                        f"{SEAMS}test_seam_a_reservation_entry_carries_the_fields_the_hold_is_read_from",
                    )
                ),
            ),
            Form(
                "a hold larger than the counter",
                "spend floors at zero",
                Supported(
                    (f"{GATEWAY}test_a_hold_larger_than_the_counter_floors_at_zero",)
                ),
            ),
        ),
    ),
    Input(
        shape="team spend counter",
        source="brownout",
        parser="bridge.live_spend_reader",
        reads=("litellm:get_current_spend",),
        cites=(),
        forms=(
            Form(
                "readable",
                "spend",
                Supported(
                    (
                        f"{SEAMS}test_seam_the_spend_read_accepts_the_arguments_the_plugin_passes",
                    )
                ),
            ),
            Form(
                "unreadable",
                "refused",
                _request(
                    RuleId.SPEND_READABLE,
                    RefusalReason.SPEND_COUNTER_UNREADABLE,
                    f"{GATEWAY}test_an_unreadable_counter_refuses_and_pages_the_platform",
                ),
            ),
        ),
    ),
    Input(
        shape="the gateway's router",
        source="brownout",
        parser="bridge.live_router",
        reads=("litellm:llm_router",),
        cites=(),
        forms=(
            Form(
                "loaded",
                "model groups",
                Supported(
                    (
                        f"{BRIDGE}test_the_live_gateway_state_reads_prices_classes_and_settings",
                    )
                ),
            ),
            Form(
                "not loaded",
                "no model groups, so every model is unpriced",
                Supported(
                    (f"{BRIDGE}test_the_gateway_state_without_a_router_serves_nothing",)
                ),
            ),
        ),
    ),
    Input(
        shape="router model group",
        source="brownout",
        parser="bridge.LiteLLMGatewayState.model_group",
        reads=(
            "router:get_model_group_info",
            "router:get_model_list",
            "key:model_info",
            "member:input_cost_per_token",
            "member:output_cost_per_token",
        ),
        cites=("litellm/router.py:9177@1.95.1",),
        forms=(
            Form(
                "a group LiteLLM prices",
                "priced",
                Supported(
                    (
                        f"{BRIDGE}test_the_live_gateway_state_reads_prices_classes_and_settings",
                        f"{SEAMS}test_seam_the_router_reports_a_group_price",
                    )
                ),
            ),
            Form(
                "no group, or a group without both prices",
                "refused",
                _request(
                    RuleId.MODEL_PRICED,
                    RefusalReason.UNPRICED_MODEL,
                    f"{GATEWAY}test_an_unpriced_model_fails_closed_and_pages_the_platform",
                ),
            ),
            Form(
                "a group with an unpriced deployment beside a priced one",
                "LiteLLM reports the highest known price, so the request is admitted",
                Open("mixed-price-group"),
            ),
        ),
    ),
    Input(
        shape="router model names for a team",
        source="brownout",
        parser="bridge.LiteLLMGatewayState.serves",
        reads=("router:get_model_names",),
        cites=(),
        forms=(
            Form(
                "a name the gateway serves",
                "an incident about the group",
                Supported(
                    (
                        f"{GATEWAY}test_an_unpriced_model_group_the_gateway_serves_is_reported_per_group",
                    )
                ),
            ),
            Form(
                "a name it does not serve",
                "an incident about the team",
                Supported(
                    (
                        f"{GATEWAY}test_made_up_model_names_raise_one_incident_per_team_per_window",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="live general_settings",
        source="brownout",
        parser="bridge.live_general_settings",
        reads=("litellm:general_settings",),
        cites=(),
        forms=(
            Form(
                "the settings LiteLLM merged from its config and its database",
                "the request-time settings rules",
                Supported(
                    (
                        f"{GATEWAY}test_a_gateway_setting_violation_refuses_with_its_rule",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="downgrade permission",
        source="brownout",
        parser="bridge.LiteLLMPermissions._scope",
        reads=("litellm:_can_object_call_model", "litellm:ProxyException"),
        cites=(),
        forms=(
            Form(
                "the key and team may call the fallback",
                "degraded",
                Supported((f"{PERMISSIONS}test_an_allowlisted_target_is_served",)),
            ),
            Form(
                "the key or team may not call it",
                "the primary is served and the owner is told",
                Supported(
                    (
                        f"{PERMISSIONS}test_a_target_outside_the_team_allowlist_is_not_served",
                        f"{SEAMS}test_seam_a_model_outside_the_allowlist_raises_a_proxy_exception",
                    )
                ),
            ),
            Form(
                "the check cannot run",
                "the primary is served and the platform is paged",
                Supported(
                    (
                        f"{PERMISSIONS}test_a_check_that_cannot_run_serves_the_primary_and_pages_the_platform",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="budget reset schedule",
        source="brownout",
        parser="bridge.LiteLLMResetSchedule.window_end",
        reads=("litellm:get_budget_reset_time",),
        cites=(),
        forms=(
            Form(
                "the gateway's timezone and budget_reset_time",
                "the window",
                Supported(
                    (
                        f"{GATEWAY}test_the_window_follows_the_gateways_reset_schedule",
                        f"{SEAMS}test_seam_the_reset_schedule_follows_the_gateway_settings",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="guardrails on a downgrade",
        source="brownout",
        parser="bridge.LiteLLMGuardrails.remerge",
        reads=("litellm:_check_and_merge_model_level_guardrails",),
        cites=(),
        forms=(
            Form(
                "guardrails on the route or the fallback",
                "both routes' guardrails",
                Supported((f"{BRIDGE}test_a_rewrite_keeps_both_routes_guardrails",)),
            ),
        ),
    ),
    Input(
        shape="the Redis that dedupes announcements",
        source="brownout",
        parser="bridge.live_spend_counter_redis",
        reads=("litellm:spend_counter_cache",),
        cites=(),
        forms=(
            Form(
                "attached",
                "one announcement across replicas",
                Supported((f"{EVENTS}test_only_one_replica_wins_a_claim",)),
            ),
            Form(
                "missing or failing",
                "one announcement per replica, with a warning",
                Supported(
                    (
                        f"{EVENTS}test_a_missing_redis_warns_once",
                        f"{EVENTS}test_an_unreachable_redis_falls_back_to_replica_local_dedup",
                    )
                ),
            ),
        ),
    ),
)
