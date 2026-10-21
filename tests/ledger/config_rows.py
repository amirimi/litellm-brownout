from __future__ import annotations

from typing import Final

from brownout.refusals import RefusalReason, RuleId

from .model import Documented, Form, Input, Refused, Supported

CONFIG: Final = "tests/test_gateway_config.py::"
MALFORMED: Final = f"{CONFIG}test_a_malformed_config_is_refused_by_field"
CLI: Final = "tests/test_cli.py::"
INPUTS: Final = "tests/test_inputs.py::"
RULES: Final = "tests/test_rules.py::"
ONBOARDING: Final = "tests/test_onboarding.py::"
GATEWAY: Final = "tests/test_gateway.py::"
SEAMS: Final = "tests/test_litellm_seams.py::"
BRIDGE: Final = "tests/test_bridge.py::"
PRICE_ORDER: Final = (
    f"{BRIDGE}test_a_configured_group_is_priced_the_way_litellm_bills_it"
)


def _unreadable(*tests: str) -> Refused:
    return Refused(stage="check", rule=None, tests=tests)


CONFIG_ROWS: Final = (
    Input(
        shape="gateway config file",
        source="brownout",
        parser="cli.read_config",
        reads=(),
        cites=(),
        forms=(
            Form(
                "a YAML mapping",
                "config",
                Supported((f"{CLI}test_the_lab_passes_the_check",)),
            ),
            Form(
                "missing, unreadable, not UTF-8, not valid YAML, or not a mapping",
                "unreadable",
                _unreadable(
                    f"{CLI}test_check_refuses_a_gateway_config_it_cannot_use",
                    f"{CLI}test_init_refuses_a_gateway_config_it_cannot_use",
                    f"{CLI}test_an_unreadable_config_is_reported_by_position_only",
                ),
            ),
        ),
    ),
    Input(
        shape="config include",
        source="brownout",
        parser="inputs.with_includes",
        reads=("key:include",),
        cites=(
            "litellm/proxy/proxy_server.py:4645@1.101.6",
            "litellm/proxy/common_utils/config_includes.py:117@1.104.4",
        ),
        forms=(
            Form(
                "absent",
                "the file alone",
                Supported((f"{INPUTS}test_a_config_without_includes_is_unchanged",)),
            ),
            Form(
                "a list of files beside the config",
                "lists extend and other keys replace, as LiteLLM merges them",
                Supported(
                    (
                        f"{INPUTS}test_includes_merge_the_way_litellm_merges_them",
                        f"{CLI}test_check_reads_models_from_included_files",
                        f"{CLI}test_init_reads_models_from_included_files",
                    )
                ),
            ),
            Form(
                "not a list of paths, a missing file, or a file that is not a mapping",
                "unreadable",
                _unreadable(
                    f"{INPUTS}test_an_include_that_is_not_a_list_is_refused",
                    f"{INPUTS}test_an_include_entry_that_is_not_a_path_is_refused",
                    f"{INPUTS}test_a_missing_include_is_named",
                    f"{INPUTS}test_an_included_file_that_is_not_a_mapping_is_refused",
                ),
            ),
            Form(
                "an included file with includes of its own, which LiteLLM resolves "
                "against the root config before 1.102 and against the declaring file after",
                "refused",
                _unreadable(
                    f"{INPUTS}test_an_included_file_that_includes_more_files_is_refused"
                ),
            ),
            Form(
                "a file listed twice, which LiteLLM merges twice before 1.102 and once after",
                "refused",
                _unreadable(f"{INPUTS}test_a_file_included_twice_is_refused"),
            ),
        ),
    ),
    Input(
        shape="config general_settings and litellm_settings",
        source="brownout",
        parser="gateway_config._section",
        reads=(),
        cites=(),
        forms=(
            Form(
                "a mapping",
                "section",
                Supported((f"{CONFIG}test_the_lab_config_reads_its_deployments",)),
            ),
            Form(
                "absent or null",
                "empty",
                Supported(
                    (
                        f"{CONFIG}test_null_sections_read_as_empty",
                        f"{CONFIG}test_a_config_without_sections_reads_as_litellm_defaults",
                    )
                ),
            ),
            Form(
                "not a mapping",
                "unreadable",
                _unreadable(
                    f"{MALFORMED}[general-settings]", f"{MALFORMED}[litellm-settings]"
                ),
            ),
        ),
    ),
    Input(
        shape="config model_list",
        source="brownout",
        parser="gateway_config.read_gateway_config",
        reads=("key:model_list",),
        cites=(),
        forms=(
            Form(
                "a list of deployments",
                "deployments",
                Supported((f"{CONFIG}test_the_lab_config_reads_its_deployments",)),
            ),
            Form(
                "absent or null",
                "no deployments",
                Supported((f"{CONFIG}test_null_sections_read_as_empty",)),
            ),
            Form("not a list", "unreadable", _unreadable(f"{MALFORMED}[model-list]")),
        ),
    ),
    Input(
        shape="config model_list deployment",
        source="brownout",
        parser="gateway_config._deployment",
        reads=("key:model_name", "key:litellm_params", "key:model", "key:model_info"),
        cites=(),
        forms=(
            Form(
                "a mapping with a model_name and a litellm_params.model",
                "deployment",
                Supported((f"{CONFIG}test_a_readable_deployment_is_kept_as_written",)),
            ),
            Form(
                "not a mapping, no model_name string, no litellm_params.model, or a "
                "litellm_params or model_info that is not a mapping",
                "unreadable",
                _unreadable(
                    f"{MALFORMED}[deployment]",
                    f"{MALFORMED}[model-name-missing]",
                    f"{MALFORMED}[model-name-a-number]",
                    f"{MALFORMED}[litellm-params]",
                    f"{MALFORMED}[model-missing]",
                    f"{MALFORMED}[model-info]",
                ),
            ),
        ),
    ),
    Input(
        shape="config litellm_params and model_info input_cost_per_token and "
        "output_cost_per_token",
        source="brownout",
        parser="gateway_config._require_readable_price",
        reads=(),
        cites=(
            "litellm/router.py:322@1.101.6",
            "litellm/router.py:8653@1.101.6",
            "litellm/types/router.py:564@1.101.6",
        ),
        forms=(
            Form(
                "a number or a numeric string such as 1e-4",
                "price",
                Supported(
                    (
                        f"{CONFIG}test_a_price_is_read_the_way_litellm_reads_it",
                        f"{CLI}test_a_string_price_is_read_the_way_litellm_reads_it",
                    )
                ),
            ),
            Form(
                "absent or null in both",
                "the cost map's price",
                Supported(
                    (
                        f"{CLI}test_a_deployment_without_explicit_prices_is_priced_from_the_cost_map",
                        f"{PRICE_ORDER}[neither]",
                    )
                ),
            ),
            Form(
                "set only in model_info",
                "the model_info price",
                Supported(
                    (
                        f"{PRICE_ORDER}[model-info-only]",
                        f"{CLI}test_a_deployment_priced_in_model_info_is_priced_as_litellm_bills_it",
                        f"{CLI}test_the_lab_passes_with_its_prices_moved_into_model_info",
                        f"{SEAMS}test_seam_a_deployment_reports_the_price_litellm_bills[model-info]",
                    )
                ),
            ),
            Form(
                "one price in litellm_params and the other in model_info",
                "each price from where it is set",
                Supported((f"{PRICE_ORDER}[split-across-both]",)),
            ),
            Form(
                "the same price in both, with different values",
                "the litellm_params price, as LiteLLM bills it",
                Supported(
                    (
                        f"{PRICE_ORDER}[litellm-params-first]",
                        f"{SEAMS}test_seam_a_deployment_reports_the_price_litellm_bills[litellm-params-first]",
                    )
                ),
            ),
            Form(
                "text, a boolean, an os.environ/ reference, or a value that is not "
                "finite, in either mapping",
                "unreadable",
                _unreadable(
                    f"{MALFORMED}[price-words]",
                    f"{MALFORMED}[price-boolean]",
                    f"{MALFORMED}[price-infinite]",
                    f"{MALFORMED}[price-from-environment]",
                    f"{MALFORMED}[info-price-words]",
                    f"{MALFORMED}[info-price-boolean]",
                    f"{MALFORMED}[info-price-not-a-number]",
                    f"{MALFORMED}[info-price-from-environment]",
                    f"{MALFORMED}[info-price-unreadable-under-a-readable-one]",
                    f"{CLI}test_an_explicit_price_litellm_cannot_read_is_refused",
                ),
            ),
        ),
    ),
    Input(
        shape="the router check prices the config's deployments with",
        source="brownout",
        parser="bridge._router_for",
        reads=("litellm:Router",),
        cites=("litellm/router.py:10343@1.101.6",),
        forms=(
            Form(
                "a model the cost map knows, stated prices, or a priced model in "
                "model_info.base_model",
                "the price the gateway bills",
                Supported(
                    (
                        f"{BRIDGE}test_a_configured_group_is_priced_the_way_litellm_bills_it",
                        f"{BRIDGE}test_a_configured_group_reads_its_data_class_and_the_cost_map",
                        f"{CLI}test_an_azure_deployment_priced_through_model_info_base_model_is_priced",
                        f"{SEAMS}test_seam_the_router_prices_base_model_only_from_model_info",
                        f"{SEAMS}test_seam_building_a_router_opens_no_connection",
                    )
                ),
            ),
            Form(
                "base_model written in litellm_params, or no price at all",
                "refused, naming both fixes",
                Refused(
                    stage="check",
                    rule=RuleId.ROUTE_PRICED,
                    tests=(
                        f"{CLI}test_a_base_model_in_litellm_params_leaves_the_group_unpriced",
                        f"{RULES}test_an_unpriced_group_names_both_fixes",
                    ),
                ),
            ),
            Form(
                "the cost map check's LiteLLM loaded differs from the gateway's copy",
                "check may price a model differently from the gateway",
                Documented(
                    unseen_because="each process loads its own copy when LiteLLM starts",
                    docs="docs/limits.md",
                    phrase="`check --gateway` reads the running gateway's prices instead",
                ),
            ),
        ),
    ),
    Input(
        shape="a model_list the router cannot load",
        source="brownout",
        parser="bridge._configured_router",
        reads=("litellm:BadRequestError",),
        cites=(),
        forms=(
            Form(
                "a model with no provider LiteLLM knows",
                "refused as an unusable config",
                Refused(
                    stage="check",
                    rule=None,
                    tests=(
                        f"{BRIDGE}test_a_model_list_the_router_cannot_load_names_its_entry_and_no_value",
                    ),
                ),
            ),
        ),
    ),
    Input(
        shape="the entry the router cannot load",
        source="brownout",
        parser="bridge._unloadable",
        reads=("key:litellm_params", "key:model", "litellm:BadRequestError"),
        cites=(),
        forms=(
            Form(
                "the first entry that fails on its own",
                "named by position and model, with no other value",
                Supported(
                    (
                        f"{BRIDGE}test_a_model_list_the_router_cannot_load_names_its_entry_and_no_value",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="a deployment's supported_environments",
        source="brownout",
        parser="bridge._without_environment",
        reads=("key:model_info",),
        cites=(),
        forms=(
            Form(
                "any value, including an environment check does not run in",
                "ignored, so every deployment is priced",
                Supported(
                    (
                        f"{BRIDGE}test_a_deployment_limited_to_other_environments_is_still_priced",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="deployments the router loaded",
        source="brownout",
        parser="bridge.configured_groups",
        reads=("key:model_name",),
        cites=(),
        forms=(
            Form(
                "every loaded deployment",
                "grouped by model_name and priced through candidate_group",
                Supported(
                    (
                        f"{BRIDGE}test_a_configured_group_reads_its_data_class_and_the_cost_map",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="general_settings.fail_closed_budget_enforcement",
        source="brownout",
        parser="gateway_config.settings_from_general",
        reads=("key:fail_closed_budget_enforcement",),
        cites=("litellm/proxy/proxy_server.py:2430@1.101.6",),
        forms=(
            Form(
                "literally true",
                "on",
                Supported(
                    (f"{CONFIG}test_a_setting_is_on_only_when_it_is_literally_true",)
                ),
            ),
            Form(
                "anything else, which LiteLLM also reads as off",
                "refused",
                Refused(
                    stage="check",
                    rule=RuleId.FAIL_CLOSED_ON,
                    tests=(
                        f"{RULES}test_each_gateway_setting_has_its_rule",
                        f"{GATEWAY}test_a_gateway_setting_violation_refuses_with_its_rule",
                    ),
                    reason=RefusalReason.GATEWAY_RULE_VIOLATED,
                ),
            ),
        ),
    ),
    Input(
        shape="general_settings.disable_budget_reservation",
        source="brownout",
        parser="gateway_config.settings_from_general",
        reads=("key:disable_budget_reservation",),
        cites=("litellm/proxy/auth/user_api_key_auth.py:2708@1.101.6",),
        forms=(
            Form(
                "literally true",
                "refused",
                Refused(
                    stage="check",
                    rule=RuleId.RESERVATION_ON,
                    tests=(
                        f"{RULES}test_each_gateway_setting_has_its_rule",
                        f"{ONBOARDING}test_a_vanilla_config_gets_every_mechanical_fix",
                    ),
                ),
            ),
            Form(
                "anything else, which LiteLLM also reads as reservations on",
                "on",
                Supported(
                    (f"{CONFIG}test_a_setting_is_on_only_when_it_is_literally_true",)
                ),
            ),
        ),
    ),
    Input(
        shape="general_settings.coordination_redis",
        source="brownout",
        parser="gateway_config.read_gateway_config",
        reads=("key:coordination_redis",),
        cites=("litellm/proxy/proxy_server.py:4243@1.101.6",),
        forms=(
            Form(
                "a mapping that names a connection",
                "Redis",
                Supported(
                    (
                        f"{CONFIG}test_coordination_redis_counts_when_it_names_a_connection",
                    )
                ),
            ),
            Form(
                "absent or empty, including one saved only through the admin UI",
                "refused",
                Refused(
                    stage="check",
                    rule=RuleId.REDIS_CONFIGURED,
                    tests=(
                        f"{RULES}test_a_missing_redis_is_a_file_violation",
                        f"{ONBOARDING}test_a_missing_redis_violation_carries_the_block_to_add",
                    ),
                ),
            ),
            Form(
                "not a mapping",
                "unreadable",
                _unreadable(f"{MALFORMED}[coordination-redis]"),
            ),
        ),
    ),
    Input(
        shape="litellm_settings.cache_params",
        source="brownout",
        parser="gateway_config.read_gateway_config",
        reads=("key:cache_params",),
        cites=(),
        forms=(
            Form(
                "a mapping or absent",
                "cache settings",
                Supported((f"{CONFIG}test_a_redis_cache_offers_its_connection",)),
            ),
            Form(
                "not a mapping", "unreadable", _unreadable(f"{MALFORMED}[cache-params]")
            ),
        ),
    ),
    Input(
        shape="litellm_settings.cache_params Redis connection",
        source="brownout",
        parser="gateway_config._cache_redis",
        reads=(
            "key:type",
            "key:host",
            "key:port",
            "key:url",
            "key:username",
            "key:password",
            "key:ssl",
            "key:db",
            "key:startup_nodes",
            "key:sentinel_nodes",
            "key:service_name",
            "key:sentinel_password",
        ),
        cites=(),
        forms=(
            Form(
                "type redis with a host, url, startup_nodes or sentinel_nodes",
                "init copies its connection into coordination_redis",
                Supported(
                    (
                        f"{CONFIG}test_a_redis_cache_offers_its_connection",
                        f"{ONBOARDING}test_a_vanilla_config_gets_every_mechanical_fix",
                    )
                ),
            ),
            Form(
                "any other cache",
                "nothing to copy",
                Supported(
                    (
                        f"{ONBOARDING}test_no_coordination_redis_is_derived_without_a_redis_target",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="litellm_settings.callbacks",
        source="brownout",
        parser="gateway_config.read_gateway_config",
        reads=("key:callbacks",),
        cites=(),
        forms=(
            Form(
                "a list of names, or one name",
                "callbacks",
                Supported((f"{CONFIG}test_callbacks_read_as_a_list",)),
            ),
            Form(
                "absent",
                "no callbacks",
                Supported(
                    (
                        f"{CONFIG}test_a_config_without_sections_reads_as_litellm_defaults",
                    )
                ),
            ),
            Form(
                "anything else",
                "unreadable",
                _unreadable(f"{MALFORMED}[callbacks]", f"{MALFORMED}[callback-entry]"),
            ),
        ),
    ),
    Input(
        shape="brownout's callback in litellm_settings.callbacks",
        source="brownout",
        parser="onboarding.config_plan",
        reads=("key:brownout.gateway.handler",),
        cites=(),
        forms=(
            Form(
                "listed",
                "nothing to add",
                Supported((f"{ONBOARDING}test_a_ready_config_needs_no_change",)),
            ),
            Form(
                "missing",
                "init adds it",
                Supported(
                    (f"{ONBOARDING}test_a_vanilla_config_gets_every_mechanical_fix",)
                ),
            ),
        ),
    ),
    Input(
        shape="litellm_settings.budget_exceeded_status_code",
        source="brownout",
        parser="gateway_config.read_gateway_config",
        reads=("key:budget_exceeded_status_code",),
        cites=(),
        forms=(
            Form(
                "set to any value",
                "kept",
                Supported((f"{CONFIG}test_a_status_code_counts_when_it_is_set",)),
            ),
            Form(
                "absent",
                "init recommends 429",
                Supported(
                    (f"{ONBOARDING}test_a_vanilla_config_gets_every_mechanical_fix",)
                ),
            ),
        ),
    ),
    Input(
        shape="model_info.data_class",
        source="brownout",
        parser="gateway_config.declared_data_class",
        reads=("key:data_class",),
        cites=(),
        forms=(
            Form(
                "a string in the manifest's data_class_order",
                "data class",
                Supported((f"{RULES}test_a_served_priced_classified_pair_passes",)),
            ),
            Form(
                "any value while the manifest declares no data_class_order",
                "ignored",
                Supported(
                    (
                        f"{RULES}test_without_a_data_class_order_a_pairing_needs_only_to_be_served_and_priced",
                    )
                ),
            ),
            Form(
                "absent or not a string on a route or a fallback, while the manifest "
                "declares a data_class_order",
                "refused",
                Refused(
                    stage="check",
                    rule=RuleId.DATA_CLASS_DECLARED,
                    tests=(
                        f"{CONFIG}test_a_data_class_is_a_string_or_undeclared",
                        f"{RULES}test_deployments_that_disagree_on_a_data_class_are_a_violation",
                        "tests/test_cli.py::test_a_fallback_without_data_classes_fails_when_the_manifest_lists_them",
                    ),
                ),
            ),
            Form(
                "outside the manifest's data_class_order",
                "refused",
                Refused(
                    stage="check",
                    rule=RuleId.DATA_CLASS_DECLARED,
                    tests=(
                        f"{RULES}test_a_data_class_outside_the_order_names_the_fix",
                    ),
                ),
            ),
        ),
    ),
)
