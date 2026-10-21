from __future__ import annotations

from typing import Final

from brownout.refusals import RefusalReason, RuleId

from .model import Documented, Form, Input, Open, Refused, Supported

SEAMS: Final = "tests/test_litellm_seams.py::"
MANIFEST: Final = "tests/test_manifest.py::"
BRIDGE: Final = "tests/test_bridge.py::"
RULES: Final = "tests/test_rules.py::"
CLI: Final = "tests/test_cli.py::"
SMOKE: Final = "tests/test_smoke_e2e.py::"
GATEWAY: Final = "tests/test_gateway.py::"
NAME_NOT_A_GROUP: Final = Refused(
    stage="request",
    rule=RuleId.MODEL_IS_A_GROUP,
    reason=RefusalReason.MODEL_NOT_A_GROUP,
    tests=(
        f"{GATEWAY}test_a_name_that_is_not_a_model_group_is_refused",
        "tests/test_bridge.py::test_only_a_configured_model_name_is_a_concrete_group",
        f"{SMOKE}test_a_router_alias_is_refused_before_routing",
    ),
)
BUDGETS_BEFORE_TIERS: Final = Open("budgets-before-tiers")


LITELLM_ROWS: Final = (
    Input(
        shape="model rewrites LiteLLM applies before the hook",
        source="litellm",
        parser=None,
        reads=(),
        cites=(
            "litellm/proxy/common_request_processing.py:1899@1.101.6",
            "litellm/proxy/litellm_pre_call_utils.py:2558@1.101.6",
            "litellm/proxy/common_request_processing.py:2081@1.104.4",
            "litellm/proxy/common_request_processing.py:2097@1.105.2",
            "litellm/proxy/common_utils/http_parsing_utils.py:61@1.105.2",
        ),
        forms=(
            Form(
                "general_settings.completion_model, litellm_settings.model_alias_map, a "
                "key's aliases or a team's model aliases",
                "brownout sees the model LiteLLM rewrote the request to",
                Supported(
                    (
                        f"{SEAMS}test_seam_the_server_default_model_applies_before_the_pre_call_hook",
                        f"{SEAMS}test_seam_server_and_key_aliases_apply_before_the_pre_call_hook",
                        f"{SEAMS}test_seam_team_and_key_aliases_apply_before_the_pre_call_hook",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="router_settings.model_group_alias in the config",
        source="litellm",
        parser=None,
        reads=(),
        cites=("litellm/router.py:10649@1.101.6",),
        forms=(
            Form(
                "an alias for a routed group",
                "refused before routing",
                NAME_NOT_A_GROUP,
            ),
            Form("a hidden alias", "refused before routing", NAME_NOT_A_GROUP),
        ),
    ),
    Input(
        shape="a key's or team's router_settings.model_group_alias",
        source="litellm",
        parser=None,
        reads=(),
        cites=("litellm/proxy/common_request_processing.py:2147@1.104.4",),
        forms=(
            Form(
                "an alias",
                "resolved before the hook, so brownout sees the group",
                Supported(
                    (
                        f"{SEAMS}test_seam_key_and_team_router_aliases_apply_before_the_pre_call_hook",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="wildcard deployments",
        source="litellm",
        parser=None,
        reads=(),
        cites=("litellm/router.py:10640@1.101.6",),
        forms=(
            Form(
                "a wildcard route or fallback in the manifest",
                "refused",
                Refused(
                    stage="check",
                    rule=RuleId.MANIFEST_SCHEMA,
                    tests=(
                        f"{MANIFEST}test_a_wildcard_group_cannot_be_a_route_or_a_fallback",
                    ),
                ),
            ),
            Form(
                "a request for a concrete name a wildcard deployment serves",
                "refused before routing",
                NAME_NOT_A_GROUP,
            ),
        ),
    ),
    Input(
        shape="router_settings.routing_groups",
        source="litellm",
        parser=None,
        reads=(),
        cites=("litellm/router.py:11711@1.104.4",),
        forms=(
            Form(
                "a request for a routing group's name",
                "refused before routing",
                NAME_NOT_A_GROUP,
            ),
        ),
    ),
    Input(
        shape="a comma-separated model list",
        source="litellm",
        parser=None,
        reads=(),
        cites=("litellm/proxy/route_llm_request.py:503@1.101.6",),
        forms=(
            Form(
                "a request naming several models at once",
                "refused before routing",
                NAME_NOT_A_GROUP,
            ),
        ),
    ),
    Input(
        shape="a team's own models",
        source="litellm",
        parser=None,
        reads=(),
        cites=(
            "litellm/proxy/route_llm_request.py:632@1.101.6",
            "litellm/router.py:11018@1.101.6",
        ),
        forms=(
            Form(
                "a request for a team's public model name",
                "refused before routing",
                NAME_NOT_A_GROUP,
            ),
        ),
    ),
    Input(
        shape="a group's prices across its deployments",
        source="litellm",
        parser=None,
        reads=(),
        cites=("litellm/router.py:10536@1.101.6",),
        forms=(
            Form(
                "every deployment priced",
                "priced",
                Supported((f"{SEAMS}test_seam_the_router_reports_a_group_price",)),
            ),
            Form(
                "an unpriced deployment beside a priced one",
                "refused, because LiteLLM would bill that deployment nothing",
                Refused(
                    stage="request",
                    rule=RuleId.MODEL_PRICED,
                    reason=RefusalReason.UNPRICED_MODEL,
                    tests=(
                        "tests/test_rules.py::test_a_group_with_one_unpriced_deployment_is_unpriced",
                        f"{GATEWAY}test_the_filter_refuses_an_unpriced_candidate",
                        f"{SMOKE}test_a_group_with_an_unpriced_deployment_is_refused",
                    ),
                ),
            ),
            Form(
                "prices only in model_info",
                "priced at request time, as LiteLLM bills it",
                Supported(
                    (
                        "tests/test_bridge.py::test_a_group_is_priced_by_what_litellm_bills_each_deployment",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="a model whose prices are both zero",
        source="litellm",
        parser=None,
        reads=(),
        cites=("litellm/proxy/auth/user_api_key_auth.py:1568@1.101.6",),
        forms=(
            Form(
                "a request for it",
                "LiteLLM skips its budget checks, and brownout refuses it as unpriced",
                Refused(
                    stage="check",
                    rule=RuleId.MODEL_PRICED,
                    tests=(f"{RULES}test_what_counts_as_priced",),
                    reason=RefusalReason.UNPRICED_MODEL,
                ),
            ),
        ),
    ),
    Input(
        shape="router fallbacks",
        source="litellm",
        parser=None,
        reads=(),
        cites=("litellm/router.py:2675@1.101.6",),
        forms=(
            Form(
                "fallbacks in router_settings or in a key's or team's router_settings",
                "emptied on every governed request",
                Supported(
                    (
                        f"{BRIDGE}test_every_governed_request_has_its_fallbacks_emptied",
                        f"{SEAMS}test_seam_a_request_fallback_list_replaces_the_routers_own",
                        f"{SEAMS}test_seam_key_and_team_router_settings_skip_keys_the_request_holds",
                    )
                ),
            ),
            Form(
                "a local rate limit that retries the request on a router fallback",
                "stopped, because the pre-call hook sets disable_fallbacks",
                Supported(
                    (
                        f"{SMOKE}test_a_local_rate_limit_never_retries_on_a_router_fallback",
                        "tests/test_bridge.py::test_every_governed_request_has_its_fallbacks_emptied",
                        f"{SEAMS}test_seam_a_local_rate_limit_retries_the_pre_call_logic_on_a_router_fallback",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="settings saved through the admin UI",
        source="litellm",
        parser=None,
        reads=(),
        cites=("litellm/proxy/proxy_server.py:1169@1.101.6",),
        forms=(
            Form(
                "general_settings or coordination_redis kept only in LiteLLM's database",
                "check reads the config file, so it refuses them",
                Refused(
                    stage="check",
                    rule=RuleId.FAIL_CLOSED_ON,
                    tests=(f"{RULES}test_each_gateway_setting_has_its_rule",),
                ),
            ),
            Form(
                "models kept only in LiteLLM's database",
                "init and check --gateway read them from /model/info",
                Supported(
                    (f"{CLI}test_check_sees_gateway_models_a_file_does_not_list",)
                ),
            ),
        ),
    ),
    Input(
        shape="where the gateway loads its config",
        source="litellm",
        parser=None,
        reads=(),
        cites=("litellm/proxy/proxy_server.py:4645@1.101.6",),
        forms=(
            Form(
                "a bucket or a path only the gateway can reach",
                "check and init read the file they are given",
                Documented(
                    unseen_because="nothing in the config names where the gateway loaded it from",
                    docs="docs/limits.md",
                    phrase="`check` and `init` read the config file you pass them",
                ),
            ),
        ),
    ),
    Input(
        shape="LiteLLM budgets other than the team's",
        source="litellm",
        parser=None,
        reads=(),
        cites=(
            "litellm/proxy/auth/auth_checks.py:834@1.101.6",
            "litellm/proxy/auth/auth_checks.py:932@1.104.4",
        ),
        forms=(
            Form(
                "an organization max_budget",
                "refuses before the tiers",
                BUDGETS_BEFORE_TIERS,
            ),
            Form("a tag budget", "refuses before the tiers", BUDGETS_BEFORE_TIERS),
            Form(
                "an end-user budget, including litellm_settings.max_end_user_budget",
                "refuses before the tiers",
                BUDGETS_BEFORE_TIERS,
            ),
            Form(
                "the global proxy budget, litellm_settings.max_budget",
                "refuses before the tiers",
                BUDGETS_BEFORE_TIERS,
            ),
            Form(
                "a team's budget_limits windows",
                "refuses before the tiers",
                BUDGETS_BEFORE_TIERS,
            ),
            Form(
                "a member budget added after init ran",
                "refuses before the tiers",
                BUDGETS_BEFORE_TIERS,
            ),
            Form(
                "a budget LiteLLM gives new keys through default_key_generate_params",
                "refused per request by key-has-no-budget",
                BUDGETS_BEFORE_TIERS,
            ),
            Form(
                "a model access group budget",
                "refuses before the tiers",
                BUDGETS_BEFORE_TIERS,
            ),
            Form(
                "a user's personal budget with apply_user_budget_to_team_keys on",
                "refuses before the tiers",
                BUDGETS_BEFORE_TIERS,
            ),
            Form(
                "a user's personal budget on a team key otherwise",
                "never applies",
                Supported(
                    (
                        f"{SEAMS}test_seam_a_user_budget_skips_team_keys_unless_the_setting_is_true",
                    )
                ),
            ),
        ),
    ),
)
