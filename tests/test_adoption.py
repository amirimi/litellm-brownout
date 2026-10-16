from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Final

import pytest

from .adoption.runner import scenario_report
from .adoption.scenario import gaps_match, load_scenario, scenario_names
from .support import REPO_ROOT

ENABLED: Final = os.getenv("BROWNOUT_ADOPTION", "").lower() in {"1", "true", "yes"}
RELEASES: Final = tuple(os.getenv("BROWNOUT_ADOPTION_RELEASES", "v1.104.3").split(","))
SELECTED: Final = frozenset(
    name for name in os.getenv("BROWNOUT_ADOPTION_SCENARIOS", "").split(",") if name
)

pytestmark = [
    pytest.mark.adoption,
    pytest.mark.skipif(
        not ENABLED,
        reason="set BROWNOUT_ADOPTION=1 to run the adoption paths, which need Docker",
    ),
]


def chosen_scenarios() -> list[str]:
    names: Final = scenario_names()
    if not SELECTED:
        return names
    return [name for name in names if name in SELECTED]


@pytest.fixture(scope="session")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out: Final = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    built: Final = next(out.glob("litellm_brownout-*.whl"))
    subprocess.run(
        ["uvx", "--from", str(built), "brownout", "--help"],
        check=True,
        capture_output=True,
    )
    return built


@pytest.mark.parametrize("release", RELEASES)
@pytest.mark.parametrize("name", chosen_scenarios())
def test_the_printed_path_meets_only_the_declared_gaps(
    name: str, release: str, wheel: Path, tmp_path: Path
) -> None:
    scenario: Final = load_scenario(name)

    report: Final = scenario_report(scenario, release, wheel, tmp_path)

    assert gaps_match(report.gaps, scenario.gaps_on(release)), report.summary()
