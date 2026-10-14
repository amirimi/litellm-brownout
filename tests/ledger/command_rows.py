from __future__ import annotations

from typing import Final

from brownout.refusals import RuleId

from .model import Form, Input, Refused, Supported

CLI: Final = "tests/test_cli.py::"
SETTINGS: Final = "tests/test_settings.py::"
BRIDGE: Final = "tests/test_bridge.py::"
SEAMS: Final = "tests/test_litellm_seams.py::"
ONBOARDING: Final = "tests/test_onboarding.py::"


COMMAND_ROWS: Final = (
    Input(
        shape="brownout command line",
        source="brownout",
        parser="cli._parser",
        reads=(
            "flag:check",
            "flag:init",
            "flag:--gateway-config",
            "flag:--manifest",
            "flag:--gateway",
            "flag:--manifest-out",
            "flag:--config-out",
            "flag:--write",
        ),
        cites=(),
        forms=(
            Form(
                "check with a gateway config and a manifest",
                "check",
                Supported((f"{CLI}test_the_lab_passes_the_check",)),
            ),
            Form(
                "check with --gateway",
                "check with the gateway's models",
                Supported((f"{CLI}test_check_with_a_gateway_needs_the_init_key",)),
            ),
            Form(
                "init with a gateway and a gateway config",
                "init",
                Supported(
                    (
                        f"{CLI}test_init_writes_a_manifest_and_config_that_pass_the_check",
                    )
                ),
            ),
            Form(
                "init --write onto a file that exists or onto one of its inputs",
                "refused",
                Refused(
                    stage="init",
                    rule=None,
                    tests=(
                        f"{CLI}test_init_never_overwrites_an_existing_output",
                        f"{CLI}test_an_output_that_is_an_input_is_refused",
                    ),
                ),
            ),
            Form(
                "a command without its required flags",
                "refused",
                Refused(
                    stage="check",
                    rule=None,
                    tests=(
                        f"{CLI}test_a_command_without_its_required_flags_exits_two",
                    ),
                ),
            ),
        ),
    ),
    Input(
        shape="BROWNOUT_INIT_KEY",
        source="brownout",
        parser="settings.init_key",
        reads=("env:BROWNOUT_INIT_KEY",),
        cites=(),
        forms=(
            Form(
                "a key that can read the gateway",
                "init and check --gateway read the gateway",
                Supported((f"{SETTINGS}test_the_init_key_comes_from_the_environment",)),
            ),
            Form(
                "unset",
                "refused",
                Refused(
                    stage="init",
                    rule=None,
                    tests=(
                        f"{SETTINGS}test_no_init_key_is_set_by_default",
                        f"{CLI}test_init_without_its_key_exits_two",
                        f"{CLI}test_check_with_a_gateway_needs_the_init_key",
                    ),
                ),
            ),
        ),
    ),
    Input(
        shape="the init key's hash",
        source="brownout",
        parser="cli._init_command",
        reads=("litellm:hash_token",),
        cites=(),
        forms=(
            Form(
                "the key init runs with",
                "found among /key/list's tokens",
                Supported(
                    (f"{CLI}test_the_key_init_runs_with_does_not_block_readiness",)
                ),
            ),
        ),
    ),
    Input(
        shape="LiteLLM's default alert types",
        source="brownout",
        parser="cli.litellm_alert_types",
        reads=("litellm:DEFAULT_ALERT_TYPES",),
        cites=(),
        forms=(
            Form(
                "a list that includes budget_alerts",
                "the alert types a config without alert_types gets",
                Supported(
                    (
                        f"{SEAMS}test_seam_litellm_sends_budget_alerts_by_default",
                        f"{ONBOARDING}test_a_vanilla_config_gets_every_mechanical_fix",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="LiteLLM release",
        source="release",
        parser="bridge.running_litellm",
        reads=("distribution:litellm",),
        cites=(),
        forms=(
            Form(
                "below 1.101.6, or unreadable",
                "refused",
                Refused(
                    stage="start",
                    rule=RuleId.SUPPORTED_LITELLM,
                    tests=(
                        f"{BRIDGE}test_a_release_below_the_floor_refuses_to_start",
                        f"{BRIDGE}test_an_unreadable_release_refuses_to_start",
                    ),
                ),
            ),
            Form(
                "1.101.6 to 1.104",
                "runs",
                Supported((f"{BRIDGE}test_a_tested_release_runs_quietly",)),
            ),
            Form(
                "past 1.104",
                "runs, with an untested-release warning",
                Supported(
                    (f"{BRIDGE}test_a_release_past_the_ceiling_runs_with_a_warning",)
                ),
            ),
        ),
    ),
)
