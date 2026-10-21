from __future__ import annotations

from typing import Final

from brownout.refusals import RuleId

from .model import Form, Input, Refused, Supported

MANIFEST: Final = "tests/test_manifest.py::"
MALFORMED: Final = f"{MANIFEST}test_a_malformed_manifest_is_refused_by_schema"
CLI: Final = "tests/test_cli.py::"
INPUTS: Final = "tests/test_inputs.py::"
SETTINGS: Final = "tests/test_settings.py::"
LOADS: Final = f"{MANIFEST}test_the_lab_manifest_loads"


def _schema(*tests: str) -> Refused:
    return Refused(stage="check", rule=RuleId.MANIFEST_SCHEMA, tests=tests)


MANIFEST_ROWS: Final = (
    Input(
        shape="manifest file",
        source="brownout",
        parser="manifest.Manifest.from_file",
        reads=(),
        cites=(),
        forms=(
            Form(
                "missing or unreadable",
                "unreadable",
                Refused(
                    stage="check",
                    rule=None,
                    tests=(
                        f"{CLI}test_check_refuses_a_missing_manifest",
                        f"{INPUTS}test_a_missing_file_is_named",
                        f"{INPUTS}test_a_file_that_cannot_be_read_names_the_reason",
                    ),
                ),
            ),
            Form(
                "not valid YAML or not UTF-8",
                "unreadable",
                Refused(
                    stage="check",
                    rule=None,
                    tests=(
                        f"{CLI}test_check_blames_a_malformed_manifest_on_the_manifest",
                        f"{INPUTS}test_invalid_yaml_is_reported_by_position_only",
                        f"{INPUTS}test_text_that_is_not_utf8_gives_an_offset_and_no_byte",
                    ),
                ),
            ),
            Form(
                "a YAML document that is not a mapping",
                "refused",
                _schema(f"{MALFORMED}[manifest-not-a-mapping]"),
            ),
            Form(
                "an unknown top-level key",
                "refused",
                _schema(f"{MANIFEST}test_an_unknown_key_is_refused_by_name"),
            ),
        ),
    ),
    Input(
        shape="manifest teams",
        source="brownout",
        parser="manifest.Manifest.from_document",
        reads=("key:teams",),
        cites=(),
        forms=(
            Form(
                "a non-empty mapping keyed by string team ids",
                "teams",
                Supported((LOADS, f"{MANIFEST}test_a_quoted_number_is_a_team_id")),
            ),
            Form(
                "missing, empty or not a mapping",
                "refused",
                _schema(
                    f"{MALFORMED}[teams-missing]",
                    f"{MALFORMED}[teams-empty]",
                    f"{MALFORMED}[teams-not-a-mapping]",
                ),
            ),
            Form(
                "a team id YAML reads as a number or a boolean",
                "refused",
                _schema(
                    f"{MALFORMED}[team-id-a-number]", f"{MALFORMED}[team-id-a-boolean]"
                ),
            ),
        ),
    ),
    Input(
        shape="manifest data_class_order",
        source="brownout",
        parser="manifest.Manifest.from_document",
        reads=("key:data_class_order",),
        cites=(),
        forms=(
            Form("a list of distinct non-empty strings", "order", Supported((LOADS,))),
            Form(
                "absent, with or without fallbacks",
                "no order, so no data-class rule runs",
                Supported(
                    (
                        f"{MANIFEST}test_a_manifest_needs_no_data_class_order",
                        "tests/test_rules.py::test_without_a_data_class_order_a_pairing_needs_only_to_be_served_and_priced",
                        "tests/test_cli.py::test_a_fallback_without_data_classes_passes_when_the_manifest_lists_none",
                        "tests/test_gateway.py::test_without_a_data_class_order_an_unclassified_fallback_is_served",
                    )
                ),
            ),
            Form(
                "empty, not a list, or holding an empty or non-string entry",
                "refused",
                _schema(
                    f"{MALFORMED}[order-empty]",
                    f"{MALFORMED}[order-not-a-list]",
                    f"{MALFORMED}[order-entry-empty]",
                    f"{MALFORMED}[order-entry-not-a-string]",
                ),
            ),
            Form(
                "a data class listed twice",
                "refused",
                _schema(f"{MANIFEST}test_a_data_class_listed_twice_is_refused"),
            ),
        ),
    ),
    Input(
        shape="manifest teams.<team>",
        source="brownout",
        parser="manifest._build_team",
        reads=(),
        cites=(),
        forms=(
            Form("a mapping of known keys", "team", Supported((LOADS,))),
            Form(
                "not a mapping", "refused", _schema(f"{MALFORMED}[team-not-a-mapping]")
            ),
            Form(
                "an unknown key",
                "refused",
                _schema(f"{MANIFEST}test_an_unknown_key_is_refused_by_name"),
            ),
        ),
    ),
    Input(
        shape="a team's policy epoch, derived from its parsed policy",
        source="brownout",
        parser="manifest.TeamPolicy.policy_epoch",
        reads=(),
        cites=(),
        forms=(
            Form(
                "the same policy in another key order, layout, comment or number form",
                "the same epoch",
                Supported(
                    (
                        f"{MANIFEST}test_key_order_formatting_comments_and_number_form_leave_the_epoch_alone",
                        f"{MANIFEST}test_the_team_id_and_the_data_class_order_leave_the_epoch_alone",
                    )
                ),
            ),
            Form(
                "a changed envelope, window, tier, route, fallback or degrade_at",
                "a new epoch for that team alone, so its notices fire again",
                Supported(
                    (
                        f"{MANIFEST}test_a_policy_change_moves_that_teams_epoch_and_no_other",
                        "tests/test_gateway.py::test_a_tier_claimed_under_the_old_policy_fires_again_under_the_new",
                    )
                ),
            ),
            Form(
                "version or policy_epoch written by an earlier release",
                "refused by name as an unknown key",
                _schema(
                    f"{MANIFEST}test_a_manifest_that_still_carries_a_bookkeeping_key_is_refused"
                ),
            ),
        ),
    ),
    Input(
        shape="manifest teams.<team>.envelope_usd",
        source="brownout",
        parser="manifest._build_team",
        reads=("key:envelope_usd",),
        cites=(),
        forms=(
            Form("a positive finite number", "envelope", Supported((LOADS,))),
            Form(
                "zero, negative, NaN or infinite",
                "refused",
                _schema(
                    f"{MANIFEST}test_an_unusable_envelope_is_refused[0]",
                    f"{MANIFEST}test_an_unusable_envelope_is_refused[nan]",
                ),
            ),
            Form(
                "a string or a boolean",
                "refused",
                _schema(
                    f"{MANIFEST}test_an_unusable_envelope_is_refused[500]",
                    f"{MANIFEST}test_an_unusable_envelope_is_refused[True]",
                ),
            ),
        ),
    ),
    Input(
        shape="manifest teams.<team>.budget_duration",
        source="brownout",
        parser="manifest._build_team",
        reads=("key:budget_duration",),
        cites=(),
        forms=(
            Form(
                "a window that tumbles with LiteLLM's resets",
                "window",
                Supported((f"{MANIFEST}test_every_window_that_tumbles_is_accepted",)),
            ),
            Form(
                "any other value",
                "refused",
                Refused(
                    stage="check",
                    rule=RuleId.DURATION_TUMBLES,
                    tests=(f"{MANIFEST}test_a_window_that_does_not_tumble_is_refused",),
                ),
            ),
        ),
    ),
    Input(
        shape="manifest teams.<team>.warn_tiers",
        source="brownout",
        parser="manifest._build_team",
        reads=("key:warn_tiers",),
        cites=(),
        forms=(
            Form(
                "absent or null",
                "no tiers",
                Supported((f"{MANIFEST}test_team_and_route_tiers_are_optional",)),
            ),
            Form("integers from 1 to 99", "tiers", Supported((LOADS,))),
            Form(
                "a tier listed twice",
                "tiers",
                Supported((f"{MANIFEST}test_tiers_listed_twice_collapse_into_one",)),
            ),
            Form(
                "not a list, or a tier that is not an integer from 1 to 99",
                "refused",
                _schema(
                    f"{MALFORMED}[team-tiers-not-a-list]",
                    f"{MANIFEST}test_a_team_tier_outside_the_range_it_owns_is_refused",
                ),
            ),
        ),
    ),
    Input(
        shape="manifest teams.<team>.routes",
        source="brownout",
        parser="manifest._build_team",
        reads=("key:routes",),
        cites=(),
        forms=(
            Form(
                "a mapping keyed by string route names", "routes", Supported((LOADS,))
            ),
            Form(
                "absent or null",
                "no routes",
                Supported((f"{MANIFEST}test_a_team_without_routes_has_none",)),
            ),
            Form(
                "not a mapping, or a route name YAML reads as a number",
                "refused",
                _schema(
                    f"{MALFORMED}[routes-not-a-mapping]",
                    f"{MALFORMED}[route-name-a-number]",
                ),
            ),
        ),
    ),
    Input(
        shape="manifest teams.<team>.routes.<route>",
        source="brownout",
        parser="manifest._build_route",
        reads=(),
        cites=(),
        forms=(
            Form("a mapping of known keys", "route", Supported((LOADS,))),
            Form(
                "null",
                "a route that never degrades",
                Supported(
                    (
                        f"{MANIFEST}test_a_route_declared_with_no_settings_never_degrades",
                    )
                ),
            ),
            Form(
                "not a mapping, or an unknown key",
                "refused",
                _schema(
                    f"{MALFORMED}[route-not-a-mapping]",
                    f"{MANIFEST}test_an_unknown_key_is_refused_by_name",
                ),
            ),
        ),
    ),
    Input(
        shape="manifest route and fallback names",
        source="brownout",
        parser="manifest._require_concrete",
        reads=("key:*",),
        cites=(),
        forms=(
            Form("a concrete model group name", "name", Supported((LOADS,))),
            Form(
                "a name holding the wildcard *",
                "refused",
                _schema(
                    f"{MANIFEST}test_a_wildcard_group_cannot_be_a_route_or_a_fallback"
                ),
            ),
        ),
    ),
    Input(
        shape="manifest teams.<team>.routes.<route>.fallback",
        source="brownout",
        parser="manifest._build_route",
        reads=("key:fallback",),
        cites=(),
        forms=(
            Form(
                "absent",
                "no fallback",
                Supported((f"{MANIFEST}test_team_and_route_tiers_are_optional",)),
            ),
            Form("another model group's name", "fallback", Supported((LOADS,))),
            Form(
                "empty or not a string",
                "refused",
                _schema(
                    f"{MALFORMED}[fallback-empty]",
                    f"{MALFORMED}[fallback-not-a-string]",
                ),
            ),
            Form(
                "the route itself",
                "refused",
                Refused(
                    stage="check",
                    rule=RuleId.FALLBACK_DISTINCT,
                    tests=(
                        f"{MANIFEST}test_a_route_that_falls_back_to_itself_is_refused",
                    ),
                ),
            ),
        ),
    ),
    Input(
        shape="manifest teams.<team>.routes.<route>.degrade_at",
        source="brownout",
        parser="manifest._build_route",
        reads=("key:degrade_at",),
        cites=(),
        forms=(
            Form(
                "absent",
                "never degrades",
                Supported((f"{MANIFEST}test_team_and_route_tiers_are_optional",)),
            ),
            Form(
                "an integer from 1 to 99 beside a fallback",
                "threshold",
                Supported((LOADS,)),
            ),
            Form(
                "outside 1 to 99, not an integer, or a boolean",
                "refused",
                _schema(
                    f"{MANIFEST}test_a_degrade_threshold_at_one_hundred_percent_is_refused",
                    f"{MALFORMED}[degrade-at-not-an-integer]",
                    f"{MALFORMED}[degrade-at-boolean]",
                ),
            ),
            Form(
                "set with no fallback",
                "refused",
                _schema(
                    f"{MANIFEST}test_a_degrade_threshold_with_no_fallback_is_refused"
                ),
            ),
        ),
    ),
    Input(
        shape="manifest teams.<team>.routes.<route>.warn_tiers",
        source="brownout",
        parser="manifest._build_route",
        reads=("key:warn_tiers",),
        cites=(),
        forms=(
            Form(
                "absent or null",
                "no tiers",
                Supported((f"{MANIFEST}test_team_and_route_tiers_are_optional",)),
            ),
            Form("integers from 1 to 99", "tiers", Supported((LOADS,))),
            Form(
                "not a list, or a tier that is not an integer from 1 to 99",
                "refused",
                _schema(
                    f"{MALFORMED}[route-tiers-not-a-list]",
                    f"{MALFORMED}[route-tier-not-an-integer]",
                    f"{MANIFEST}test_a_route_tier_outside_the_range_it_owns_is_refused",
                ),
            ),
        ),
    ),
    Input(
        shape="existing manifest teams when init merges",
        source="brownout",
        parser="onboarding_files.append_teams",
        reads=("key:teams",),
        cites=(),
        forms=(
            Form(
                "the teams of a manifest that passed the manifest checks",
                "teams to keep",
                Supported(
                    (
                        f"{CLI}test_merging_keeps_their_manifest_and_appends_missing_teams",
                    )
                ),
            ),
            Form(
                "a manifest that fails the manifest checks",
                "refused",
                Refused(
                    stage="init",
                    rule=RuleId.MANIFEST_SCHEMA,
                    tests=(
                        f"{CLI}test_an_invalid_existing_manifest_stops_init_with_its_rule",
                    ),
                ),
            ),
        ),
    ),
    Input(
        shape="BROWNOUT_MANIFEST",
        source="brownout",
        parser="settings.manifest_path",
        reads=("env:BROWNOUT_MANIFEST",),
        cites=(),
        forms=(
            Form(
                "a path",
                "manifest path",
                Supported(
                    (f"{SETTINGS}test_the_manifest_path_comes_from_the_environment",)
                ),
            ),
            Form(
                "unset",
                "brownout.yaml in the working directory",
                Supported(
                    (
                        f"{SETTINGS}test_the_manifest_path_defaults_to_the_gateways_working_directory",
                    )
                ),
            ),
        ),
    ),
)
