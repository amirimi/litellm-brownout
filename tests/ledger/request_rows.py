from __future__ import annotations

from typing import Final

from brownout.refusals import RefusalReason, RuleId

from .model import Documented, Form, Input, Refused, Supported

GATEWAY: Final = "tests/test_gateway.py::"
BRIDGE: Final = "tests/test_bridge.py::"
MANIFEST: Final = "tests/test_manifest.py::"
PERMISSIONS: Final = "tests/test_permissions.py::"
EVENTS: Final = "tests/test_events.py::"
SEAMS: Final = "tests/test_litellm_seams.py::"
SMOKE: Final = "tests/test_smoke_e2e.py::"
KINDS: Final = "tests/test_call_kinds_e2e.py::"


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
                "text generation: chat, text completion, /v1/messages, /v1/responses "
                "and generate_content",
                "governed and downgraded past its tier",
                Supported(
                    (
                        f"{GATEWAY}test_every_text_call_is_downgraded_past_its_tier",
                        f"{BRIDGE}test_text_generation_is_chat_text_messages_responses_and_generate_content",
                        f"{KINDS}test_every_text_endpoint_is_downgraded_past_the_tier",
                    )
                ),
            ),
            Form(
                "embeddings",
                "team, key, concrete group and per-token price checked, tiers "
                "announced, never downgraded",
                Supported(
                    (
                        f"{GATEWAY}test_a_model_call_is_served_by_the_model_it_named_at_any_spend",
                        f"{BRIDGE}test_embeddings_are_the_other_model_call_priced_per_token",
                        f"{KINDS}test_embeddings_are_served_by_the_model_named_past_the_tier",
                    )
                ),
            ),
            Form(
                "another model call: images, speech, transcription, moderation, "
                "rerank, OCR or video generation",
                "team, key and concrete group checked, tiers announced, never "
                "downgraded, billed in LiteLLM's own units",
                Supported(
                    (
                        f"{GATEWAY}test_a_model_call_is_served_by_the_model_it_named_at_any_spend",
                        f"{GATEWAY}test_another_model_call_is_not_judged_on_a_per_token_price",
                        f"{BRIDGE}test_other_model_calls_are_the_endpoints_litellm_prices_in_their_own_units",
                    )
                ),
            ),
            Form(
                "compaction through /v1/responses/compact",
                "team, key, concrete group and per-token price checked, tiers "
                "announced, never downgraded",
                Supported(
                    (
                        f"{GATEWAY}test_a_model_call_is_served_by_the_model_it_named_at_any_spend",
                        f"{BRIDGE}test_compaction_is_its_own_kind",
                        f"{KINDS}test_compaction_is_served_by_the_model_named_past_the_tier",
                    )
                ),
            ),
            Form(
                "a call without a model: an MCP tool call, files, vector stores, "
                "containers, stored responses or video records",
                "team and key checked",
                Supported(
                    (
                        f"{GATEWAY}test_a_call_without_a_model_is_admitted_on_its_team_and_key",
                        f"{BRIDGE}test_calls_without_a_model_are_tools_files_stores_containers_and_records",
                        f"{KINDS}test_a_stored_response_is_read_back",
                        f"{SEAMS}test_seam_an_mcp_tool_call_runs_the_pre_call_hook_with_its_call_type",
                        f"{KINDS}test_a_governed_team_calls_an_mcp_tool",
                    )
                ),
            ),
            Form(
                "any other call type, such as provider pass-through, realtime, batches, "
                "fine-tuning, search tools or one a later release adds",
                "refused",
                _request(
                    RuleId.CALL_TYPE_GOVERNED,
                    RefusalReason.CALL_TYPE_NOT_GOVERNED,
                    f"{GATEWAY}test_a_call_type_outside_the_allow_list_is_refused",
                    f"{BRIDGE}test_a_call_type_outside_the_allow_list_has_no_kind",
                    f"{SEAMS}test_seam_every_admitted_call_type_is_one_litellm_names",
                ),
            ),
            Form(
                "an image, audio, rerank, OCR or video deployment LiteLLM cannot price",
                "served and recorded at $0",
                Documented(
                    unseen_because=(
                        "LiteLLM prices these per image, second, character, query or "
                        "page, in units that change between releases"
                    ),
                    docs="README.md",
                    phrase="a deployment LiteLLM cannot price records $0",
                ),
            ),
            Form(
                "the assistants, threads and runs endpoints",
                "never seen by the plugin",
                Documented(
                    unseen_because="LiteLLM runs no plugin hook on them",
                    docs="README.md",
                    phrase=(
                        "LiteLLM runs no plugin hook on its assistants, threads and "
                        "runs endpoints"
                    ),
                ),
            ),
            Form(
                "a text call whose fallback's server lacks the API LiteLLM translates "
                "it to",
                "the provider's error",
                Documented(
                    unseen_because=(
                        "nothing in the config or the request says which APIs a server "
                        "implements"
                    ),
                    docs="README.md",
                    phrase="without that API fails those calls with the provider's error",
                ),
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
        cites=("litellm/proxy/common_request_processing.py:1899@1.101.6",),
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
                "routing group, a wildcard-served name, a deployment id, a model list or "
                "a team's public model name",
                "refused",
                _request(
                    RuleId.MODEL_IS_A_GROUP,
                    RefusalReason.MODEL_NOT_A_GROUP,
                    f"{GATEWAY}test_a_name_that_is_not_a_model_group_is_refused",
                    f"{BRIDGE}test_only_a_configured_model_name_is_a_concrete_group",
                    f"{SMOKE}test_a_router_alias_is_refused_before_routing",
                ),
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
                        f"{GATEWAY}test_a_forged_field_on_a_model_call_is_replaced",
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
        reads=(
            "litellm:get_metadata_variable_name_from_kwargs",
            "key:spend_logs_metadata",
        ),
        cites=(),
        forms=(
            Form(
                "litellm_metadata present, as on /v1/messages and /v1/responses",
                "written into litellm_metadata, where LiteLLM keeps its own metadata",
                Supported(
                    (
                        f"{BRIDGE}test_the_field_is_written_where_litellm_keeps_its_metadata",
                        f"{SEAMS}test_seam_litellm_keeps_its_metadata_in_litellm_metadata_when_present",
                    )
                ),
            ),
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
        reads=(
            "key:metadata",
            "key:litellm_metadata",
            "key:spend_logs_metadata",
            "key:brownout",
        ),
        cites=(),
        forms=(
            Form(
                "the field in either container, as LiteLLM moves its metadata between them",
                "the field",
                Supported(
                    (
                        f"{BRIDGE}test_the_field_is_read_from_either_container",
                        f"{GATEWAY}test_the_filter_reads_the_field_from_litellm_metadata",
                    )
                ),
            ),
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
        reads=("router:get_model_list",),
        cites=("litellm/router.py:10640@1.101.6",),
        forms=(
            Form(
                "a group whose every deployment LiteLLM prices",
                "priced",
                Supported(
                    (
                        f"{BRIDGE}test_the_live_gateway_state_reads_prices_classes_and_settings",
                        f"{BRIDGE}test_a_group_is_priced_by_what_litellm_bills_each_deployment",
                    )
                ),
            ),
            Form(
                "no deployment, or any deployment without a usable price",
                "refused",
                _request(
                    RuleId.MODEL_PRICED,
                    RefusalReason.UNPRICED_MODEL,
                    f"{GATEWAY}test_an_unpriced_model_fails_closed_and_pages_the_platform",
                    f"{SMOKE}test_a_group_with_an_unpriced_deployment_is_refused",
                ),
            ),
        ),
    ),
    Input(
        shape="the price LiteLLM bills a deployment",
        source="brownout",
        parser="bridge.LiteLLMGatewayState._billed_prices",
        reads=(
            "router:get_deployment_model_info",
            "key:model_info",
            "key:id",
            "key:litellm_params",
            "key:model",
            "key:input_cost_per_token",
            "key:output_cost_per_token",
        ),
        cites=("litellm/router.py:10483@1.101.6",),
        forms=(
            Form(
                "a price from litellm_params, model_info or the cost map",
                "the billed price",
                Supported(
                    (
                        f"{SEAMS}test_seam_a_deployment_reports_the_price_litellm_bills",
                        f"{BRIDGE}test_a_group_is_priced_by_what_litellm_bills_each_deployment",
                    )
                ),
            ),
            Form(
                "no usable price, reported as 0 on 1.101.6 and as None on 1.104.2",
                "unpriced, so its group is unpriced",
                Supported(
                    (
                        f"{SEAMS}test_seam_an_unpriced_deployment_reports_no_price",
                        "tests/test_rules.py::test_a_group_with_one_unpriced_deployment_is_unpriced",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="data classes of a candidate list",
        source="brownout",
        parser="bridge.LiteLLMGatewayState.candidate_group",
        reads=("key:model_info",),
        cites=(),
        forms=(
            Form(
                "each deployment's model_info.data_class",
                "the group's data classes",
                Supported(
                    (
                        f"{BRIDGE}test_the_live_gateway_state_reads_prices_classes_and_settings",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="model names the router configures",
        source="brownout",
        parser="bridge.LiteLLMGatewayState.is_concrete",
        reads=("router:model_list", "key:model_name"),
        cites=(),
        forms=(
            Form(
                "a model_name of a configured deployment",
                "a concrete group",
                Supported(
                    (
                        f"{BRIDGE}test_only_a_configured_model_name_is_a_concrete_group",
                        f"{SEAMS}test_seam_the_router_lists_configured_model_names_only",
                    )
                ),
            ),
            Form(
                "any other name, or one holding *",
                "refused",
                _request(
                    RuleId.MODEL_IS_A_GROUP,
                    RefusalReason.MODEL_NOT_A_GROUP,
                    f"{GATEWAY}test_a_name_that_is_not_a_model_group_is_refused",
                    f"{BRIDGE}test_only_a_configured_model_name_is_a_concrete_group",
                    f"{SMOKE}test_a_router_alias_is_refused_before_routing",
                ),
            ),
        ),
    ),
    Input(
        shape="the team a routed request belongs to",
        source="brownout",
        parser="bridge.request_team",
        reads=("key:metadata", "key:litellm_metadata", "key:user_api_key_team_id"),
        cites=("litellm/proxy/litellm_pre_call_utils.py:1576@1.101.6",),
        forms=(
            Form(
                "a team in the manifest",
                "verified",
                Supported(
                    (
                        f"{GATEWAY}test_the_filter_passes_priced_candidates_of_the_decided_group",
                        f"{SEAMS}test_seam_the_proxy_writes_the_team_into_request_metadata",
                    )
                ),
            ),
            Form(
                "no team, or a team outside the manifest",
                "untouched",
                Supported((f"{GATEWAY}test_the_filter_leaves_other_teams_untouched",)),
            ),
        ),
    ),
    Input(
        shape="the kind brownout wrote into its field",
        source="brownout",
        parser="bridge.field_kind",
        reads=("key:kind",),
        cites=(),
        forms=(
            Form(
                "text, embedding or compaction",
                "candidates checked for a per-token price",
                Supported(
                    (
                        f"{GATEWAY}test_the_filter_refuses_an_unpriced_embedding_candidate",
                        f"{GATEWAY}test_the_filter_refuses_an_unpriced_compaction_candidate",
                    )
                ),
            ),
            Form(
                "another model call",
                "candidates checked for their group only",
                Supported(
                    (
                        f"{GATEWAY}test_the_filter_admits_another_model_call_without_a_per_token_price",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="brownout's decided route at routing",
        source="brownout",
        parser="bridge.decided_route",
        reads=("key:served_route",),
        cites=(),
        forms=(
            Form(
                "the route the pre-call hook decided",
                "the group every candidate must belong to",
                Supported(
                    (f"{GATEWAY}test_the_filter_passes_a_downgrade_to_its_fallback",)
                ),
            ),
            Form(
                "the model a model call named",
                "the group every candidate must belong to",
                Supported(
                    (
                        f"{GATEWAY}test_the_filter_verifies_a_model_call_against_the_group_it_named",
                    )
                ),
            ),
            Form(
                "no route, as on a call without a model",
                "passed unchanged",
                Supported((f"{GATEWAY}test_the_filter_passes_a_call_without_a_model",)),
            ),
            Form(
                "no field on a governed request",
                "refused",
                _request(
                    RuleId.MODEL_IS_A_GROUP,
                    RefusalReason.MODEL_NOT_A_GROUP,
                    f"{GATEWAY}test_the_filter_refuses_a_governed_request_brownout_never_saw",
                ),
            ),
        ),
    ),
    Input(
        shape="deployment filter hook parameters",
        source="brownout",
        parser="gateway.BrownoutHandler.async_filter_deployments",
        reads=(
            "parameter:model",
            "parameter:healthy_deployments",
            "parameter:request_kwargs",
        ),
        cites=("litellm/router.py:12414@1.101.6",),
        forms=(
            Form(
                "no healthy deployment, as when every one is cooling down",
                "left to LiteLLM, which reports that no deployment is available",
                Supported(
                    (
                        f"{GATEWAY}test_the_filter_leaves_an_empty_candidate_list_to_litellm",
                    )
                ),
            ),
            Form(
                "a governed request after the router resolved its name",
                "verified",
                Supported(
                    (
                        f"{GATEWAY}test_the_filter_passes_priced_candidates_of_the_decided_group",
                        f"{SEAMS}test_seam_the_deployment_filter_hook_keeps_its_parameters",
                        f"{SEAMS}test_seam_the_router_filters_deployments_after_resolving_the_name",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="candidate deployments",
        source="brownout",
        parser="gateway.BrownoutHandler._verify",
        reads=("key:model_name",),
        cites=(),
        forms=(
            Form(
                "every candidate in the decided group and priced",
                "passed unchanged",
                Supported(
                    (
                        f"{GATEWAY}test_the_filter_passes_priced_candidates_of_the_decided_group",
                        f"{GATEWAY}test_the_filter_passes_a_downgrade_to_its_fallback",
                    )
                ),
            ),
            Form(
                "a candidate from another group",
                "refused",
                _request(
                    RuleId.MODEL_IS_A_GROUP,
                    RefusalReason.MODEL_NOT_A_GROUP,
                    f"{GATEWAY}test_the_filter_refuses_a_candidate_from_another_group",
                ),
            ),
            Form(
                "a candidate without a usable price",
                "refused",
                _request(
                    RuleId.MODEL_PRICED,
                    RefusalReason.UNPRICED_MODEL,
                    f"{GATEWAY}test_the_filter_refuses_an_unpriced_candidate",
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
