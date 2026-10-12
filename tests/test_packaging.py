from __future__ import annotations

import tomllib
from typing import Any

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

from brownout import bridge

from .support import REPO_ROOT


def _pyproject() -> dict[str, Any]:
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)


def _declared_litellm() -> Requirement:
    requirements = [
        Requirement(line) for line in _pyproject()["project"]["dependencies"]
    ]
    matching = [
        requirement for requirement in requirements if requirement.name == "litellm"
    ]
    assert len(matching) == 1, "pyproject.toml must declare litellm exactly once"
    return matching[0]


def _floor() -> str:
    return ".".join(map(str, bridge.SUPPORTED_FLOOR))


def test_the_declared_litellm_range_is_the_supported_range():
    major, minor = bridge.TESTED_CEILING

    assert _declared_litellm().specifier == SpecifierSet(
        f">={_floor()},<{major}.{minor + 1}"
    )


def test_the_lock_resolves_litellm_at_the_floor():
    constraints = _pyproject()["tool"]["uv"]["constraint-dependencies"]

    assert f"litellm=={_floor()}" in constraints
