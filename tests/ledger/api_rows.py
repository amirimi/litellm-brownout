from __future__ import annotations

from typing import Final

from brownout.refusals import RuleId

from .model import Form, Input, Refused, Supported

API: Final = "tests/test_gateway_api.py::"
UNREADABLE: Final = f"{API}test_an_unreadable_gateway_raises_one_error_type"
ONBOARDING: Final = "tests/test_onboarding.py::"
CLI: Final = "tests/test_cli.py::"
RULES: Final = "tests/test_rules.py::"


def _unreadable(*tests: str) -> Refused:
    return Refused(stage="init", rule=None, tests=tests)


API_ROWS: Final = (
    Input(
        shape="a manifest route's model group",
        source="brownout",
        parser="rules._served_and_priced",
        reads=(),
        cites=(),
        forms=(
            Form(
                "a group the gateway serves with both prices",
                "served and priced",
                Supported((f"{RULES}test_a_served_priced_classified_pair_passes",)),
            ),
            Form(
                "a group neither the config nor the gateway serves",
                "refused",
                Refused(
                    stage="check",
                    rule=RuleId.ROUTE_SERVED,
                    tests=(f"{RULES}test_an_unserved_fallback_is_a_violation",),
                ),
            ),
            Form(
                "a group kept only in LiteLLM's database",
                "served when check reads the gateway",
                Supported(
                    (f"{CLI}test_check_sees_gateway_models_a_file_does_not_list",)
                ),
            ),
            Form(
                "a group missing a price on any deployment",
                "refused",
                Refused(
                    stage="check",
                    rule=RuleId.ROUTE_PRICED,
                    tests=(
                        f"{RULES}test_an_unpriced_fallback_is_a_violation",
                        f"{CLI}test_a_group_is_unpriced_when_any_deployment_is",
                    ),
                ),
            ),
        ),
    ),
    Input(
        shape="a route's fallback data class",
        source="brownout",
        parser="rules.route_rules",
        reads=(),
        cites=(),
        forms=(
            Form(
                "as restricted as the route or more",
                "fallback allowed",
                Supported((f"{RULES}test_a_dearer_fallback_is_the_route_owners_call",)),
            ),
            Form(
                "less restricted than the route",
                "refused",
                Refused(
                    stage="check",
                    rule=RuleId.FALLBACK_DATA_CLASS,
                    tests=(f"{RULES}test_a_less_restricted_fallback_is_a_violation",),
                ),
            ),
        ),
    ),
    Input(
        shape="/team/list",
        source="brownout",
        parser="gateway_api._teams",
        reads=("endpoint:/team/list",),
        cites=(),
        forms=(
            Form(
                "a list of teams",
                "teams",
                Supported((f"{API}test_teams_keep_their_budget_window_and_models",)),
            ),
            Form(
                "an error status, a transport failure, or not a list",
                "unreadable",
                _unreadable(
                    UNREADABLE,
                    f"{API}test_a_transport_failure_raises_the_same_error_type",
                    f"{CLI}test_an_unreadable_gateway_exits_two",
                ),
            ),
        ),
    ),
    Input(
        shape="/team/list team",
        source="brownout",
        parser="gateway_api._team",
        reads=(
            "key:team_id",
            "key:max_budget",
            "key:budget_duration",
            "key:models",
            "key:team_memberships",
        ),
        cites=(),
        forms=(
            Form(
                "a positive max_budget and a window the manifest accepts",
                "a manifest entry",
                Supported(
                    (
                        f"{ONBOARDING}test_a_budgeted_team_becomes_an_entry_that_matches_litellm",
                    )
                ),
            ),
            Form(
                "no positive max_budget",
                "refused",
                Refused(
                    stage="init",
                    rule=RuleId.TEAM_IN_MANIFEST,
                    tests=(
                        f"{ONBOARDING}test_a_team_without_a_budget_is_a_finding_with_a_placeholder_command",
                    ),
                ),
            ),
            Form(
                "a window the manifest does not accept",
                "refused",
                Refused(
                    stage="init",
                    rule=RuleId.DURATION_TUMBLES,
                    tests=(
                        f"{ONBOARDING}test_a_team_with_a_rejected_duration_is_a_tumbles_finding",
                    ),
                ),
            ),
            Form(
                "a max_budget or window that differs from the manifest's",
                "refused",
                Refused(
                    stage="init",
                    rule=RuleId.CEILING_MATCHES_ENVELOPE,
                    tests=(
                        f"{ONBOARDING}test_reconciling_names_every_mismatch_with_litellm",
                    ),
                ),
            ),
            Form(
                "a window that differs from the manifest's",
                "refused",
                Refused(
                    stage="init",
                    rule=RuleId.DURATION_MATCHES,
                    tests=(
                        f"{ONBOARDING}test_reconciling_names_every_mismatch_with_litellm",
                    ),
                ),
            ),
            Form(
                "a manifest team LiteLLM does not have",
                "refused",
                Refused(
                    stage="init",
                    rule=RuleId.TEAM_EXISTS,
                    tests=(
                        f"{ONBOARDING}test_reconciling_names_every_mismatch_with_litellm",
                    ),
                ),
            ),
            Form(
                "not a mapping, or no string team_id",
                "unreadable",
                _unreadable(f"{UNREADABLE}[team-without-id]"),
            ),
        ),
    ),
    Input(
        shape="/team/list team models",
        source="brownout",
        parser="onboarding._unlisted_fallbacks",
        reads=("key:all-proxy-models",),
        cites=(),
        forms=(
            Form(
                "empty, or all-proxy-models",
                "every model, fallbacks included",
                Supported(
                    (
                        f"{ONBOARDING}test_a_team_that_allows_every_model_allows_its_fallbacks",
                    )
                ),
            ),
            Form(
                "a list without a route's fallback",
                "refused",
                Refused(
                    stage="init",
                    rule=RuleId.FALLBACK_IN_TEAM_MODELS,
                    tests=(
                        f"{ONBOARDING}test_reconciling_names_every_mismatch_with_litellm",
                    ),
                ),
            ),
        ),
    ),
    Input(
        shape="/team/list team_memberships",
        source="brownout",
        parser="gateway_api._budgeted_members",
        reads=("key:user_id",),
        cites=(),
        forms=(
            Form(
                "members without a budget inside the team",
                "nothing to clear",
                Supported(
                    (f"{API}test_members_with_a_budget_inside_the_team_are_named",)
                ),
            ),
            Form(
                "a member with a budget inside the team",
                "refused",
                Refused(
                    stage="init",
                    rule=RuleId.MEMBER_HAS_NO_BUDGET,
                    tests=(
                        f"{API}test_members_with_a_budget_inside_the_team_are_named",
                        f"{ONBOARDING}test_a_member_budget_inside_a_team_is_a_finding_with_its_clearing_command",
                    ),
                ),
            ),
            Form(
                "not a list",
                "unreadable",
                _unreadable(f"{UNREADABLE}[memberships-not-a-list]"),
            ),
        ),
    ),
    Input(
        shape="/team/list membership budget table",
        source="brownout",
        parser="gateway_api._member_budget",
        reads=("key:litellm_budget_table", "key:max_budget"),
        cites=(),
        forms=(
            Form(
                "a table with a max_budget",
                "a member budget",
                Supported(
                    (f"{API}test_members_with_a_budget_inside_the_team_are_named",)
                ),
            ),
            Form(
                "no table, or a table without a max_budget",
                "no member budget",
                Supported(
                    (f"{API}test_members_with_a_budget_inside_the_team_are_named",)
                ),
            ),
        ),
    ),
    Input(
        shape="/key/list",
        source="brownout",
        parser="gateway_api._keys",
        reads=("endpoint:/key/list", "key:keys", "key:total_pages"),
        cites=(),
        forms=(
            Form(
                "pages of keys",
                "every key on every page",
                Supported(
                    (f"{API}test_every_key_page_is_read_and_own_budgets_are_named",)
                ),
            ),
            Form(
                "not a page of keys",
                "unreadable",
                _unreadable(f"{UNREADABLE}[keys-not-a-page]"),
            ),
        ),
    ),
    Input(
        shape="/key/list key",
        source="brownout",
        parser="gateway_api._key",
        reads=(
            "key:token",
            "key:key_alias",
            "key:team_id",
            "key:max_budget",
            "key:model_max_budget",
            "key:budget_limits",
        ),
        cites=(),
        forms=(
            Form(
                "a team key without a budget of its own",
                "governed through its team",
                Supported(
                    (f"{API}test_every_key_page_is_read_and_own_budgets_are_named",)
                ),
            ),
            Form(
                "a key without a team",
                "refused",
                Refused(
                    stage="init",
                    rule=RuleId.TEAM_IN_MANIFEST,
                    tests=(
                        f"{ONBOARDING}test_teamless_and_self_budgeted_keys_are_findings_with_runnable_commands",
                    ),
                ),
            ),
            Form(
                "a key with its own max_budget, model_max_budget or budget_limits",
                "refused",
                Refused(
                    stage="init",
                    rule=RuleId.KEY_HAS_NO_BUDGET,
                    tests=(
                        f"{ONBOARDING}test_teamless_and_self_budgeted_keys_are_findings_with_runnable_commands",
                    ),
                ),
            ),
            Form(
                "the key init runs with, without a team",
                "named in the admin keys notice",
                Supported(
                    (
                        f"{ONBOARDING}test_the_key_init_runs_with_is_left_to_the_admin_keys_notice",
                    )
                ),
            ),
            Form(
                "not a mapping, or no string token",
                "unreadable",
                _unreadable(f"{UNREADABLE}[key-without-token]"),
            ),
        ),
    ),
    Input(
        shape="/model/info",
        source="brownout",
        parser="gateway_api._deployments",
        reads=("endpoint:/model/info", "key:data"),
        cites=(),
        forms=(
            Form(
                "a list of deployments",
                "deployments",
                Supported(
                    (
                        f"{API}test_model_groups_carry_prices_and_data_classes_from_the_gateway",
                    )
                ),
            ),
            Form(
                "an error status, not a list, or an entry that is not a mapping",
                "unreadable",
                _unreadable(UNREADABLE, f"{UNREADABLE}[deployment-not-a-mapping]"),
            ),
        ),
    ),
    Input(
        shape="/model/info deployment",
        source="brownout",
        parser="gateway_api._groups",
        reads=("key:model_name", "key:model_info"),
        cites=(),
        forms=(
            Form(
                "deployments grouped by model_name",
                "model groups",
                Supported((f"{API}test_the_snapshot_carries_the_same_model_groups",)),
            ),
        ),
    ),
    Input(
        shape="/model/info prices",
        source="brownout",
        parser="gateway_api._group",
        reads=("key:input_cost_per_token", "key:output_cost_per_token"),
        cites=(),
        forms=(
            Form(
                "both prices on every deployment, at least one above zero",
                "priced",
                Supported(
                    (
                        f"{API}test_a_model_group_is_priced_only_when_every_deployment_is",
                    )
                ),
            ),
            Form(
                "a missing price on any deployment, or both at zero as 1.95.1 reports an "
                "unpriced model",
                "refused",
                Refused(
                    stage="init",
                    rule=RuleId.MODEL_PRICED,
                    tests=(
                        f"{API}test_a_model_is_priced_by_the_same_rule_the_requests_use",
                        f"{ONBOARDING}test_an_unpriced_model_is_a_finding_with_the_lines_to_add",
                    ),
                ),
            ),
        ),
    ),
)
