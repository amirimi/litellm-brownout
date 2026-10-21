from __future__ import annotations

from typing import Final

from .model import Form, Input, Refused, Supported

COMPOSE: Final = "tests/test_compose_override.py::"
CLI: Final = "tests/test_cli.py::"
REFUSED_SERVICE: Final = (
    f"{COMPOSE}test_a_service_brownout_cannot_extend_cleanly_is_refused"
)


COMPOSE_ROWS: Final = (
    Input(
        shape="docker compose config output",
        source="compose",
        parser="compose_override.read_compose_project",
        reads=("key:services",),
        cites=(),
        forms=(
            Form(
                "the project's resolved services",
                "the services init can extend",
                Supported(
                    (
                        f"{COMPOSE}test_compose_merges_the_override_into_a_renamed_service_with_its_own_pythonpath",
                    )
                ),
            ),
            Form(
                "docker missing, or compose config failing",
                "the override printed for the operator to save",
                Supported(
                    (
                        f"{COMPOSE}test_a_missing_docker_makes_compose_unavailable",
                        f"{COMPOSE}test_a_failing_compose_config_makes_compose_unavailable",
                    )
                ),
            ),
        ),
    ),
    Input(
        shape="a Compose service",
        source="compose",
        parser="compose_override._service",
        reads=("key:image", "key:environment", "key:volumes", "key:target"),
        cites=(),
        forms=(
            Form(
                "a service with its image, environment and mounts",
                "the service the override extends",
                Supported(
                    (
                        f"{COMPOSE}test_the_override_adds_the_plugin_to_the_one_litellm_service",
                    )
                ),
            ),
            Form(
                "a service that already mounts /app/plugins or /app/brownout.yaml",
                "refused",
                Refused(stage="init", rule=None, tests=(REFUSED_SERVICE,)),
            ),
        ),
    ),
    Input(
        shape="a service's image reference",
        source="compose",
        parser="compose_override._repository",
        reads=("key:/",),
        cites=(),
        forms=(
            Form(
                "a LiteLLM image by tag or digest, including its -database variant",
                "the LiteLLM service",
                Supported((f"{COMPOSE}test_every_litellm_image_is_recognised",)),
            ),
            Form(
                "no LiteLLM image, or several, without --service",
                "refused",
                Refused(
                    stage="init",
                    rule=None,
                    tests=(
                        REFUSED_SERVICE,
                        f"{CLI}test_init_writes_nothing_when_it_cannot_pick_the_service",
                    ),
                ),
            ),
        ),
    ),
    Input(
        shape="the service's PYTHONPATH",
        source="compose",
        parser="compose_override._pythonpath",
        reads=("key:PYTHONPATH", "key:/app/plugins"),
        cites=(),
        forms=(
            Form(
                "unset",
                "/app/plugins",
                Supported(
                    (
                        f"{COMPOSE}test_the_override_adds_the_plugin_to_the_one_litellm_service",
                    )
                ),
            ),
            Form(
                "set in environment or env_file",
                "its entries with /app/plugins appended",
                Supported(
                    (
                        f"{COMPOSE}test_a_pythonpath_the_service_already_sets_keeps_its_entries",
                        f"{COMPOSE}test_compose_merges_the_override_into_a_renamed_service_with_its_own_pythonpath",
                    )
                ),
            ),
        ),
    ),
)
