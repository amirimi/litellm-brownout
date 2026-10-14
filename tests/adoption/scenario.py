from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

SCENARIOS: Final = Path(__file__).resolve().parent / "scenarios"
BASE_COMPOSE: Final = SCENARIOS / "compose.base.yml"
ANY_SUBJECT: Final = "*"
EVERY_RELEASE: Final = "*"


@dataclass(frozen=True)
class Choice:
    rule: str
    subject: str
    values: Mapping[str, str]

    def applies_to(
        self, rule: str, subject: str, resolve: Callable[[str], str]
    ) -> bool:
        if self.rule != rule:
            return False
        return self.subject == ANY_SUBJECT or resolve(self.subject) == subject


@dataclass(frozen=True)
class Provision:
    placeholder: str
    gap: str
    compose: Path
    value: str


@dataclass(frozen=True)
class Call:
    key: str
    model: str


@dataclass(frozen=True)
class Fallback:
    team: str
    route: str
    fallback: str
    degrade_at: int
    data_class: str


@dataclass(frozen=True)
class Scenario:
    name: str
    directory: Path
    description: str
    seed: Mapping[str, Any]
    choices: tuple[Choice, ...]
    provisions: Mapping[str, Provision]
    confirm: Call
    fallback: Fallback
    drive: Call
    expected_gaps: Mapping[str, tuple[str, ...]]
    compose_files: tuple[Path, ...]
    environment: Mapping[str, str]
    init_arguments: str
    templates: tuple[str, ...]

    def gaps_on(self, release: str) -> tuple[str, ...]:
        if release in self.expected_gaps:
            return self.expected_gaps[release]
        return self.expected_gaps[EVERY_RELEASE]

    def values_for(
        self, rule: str, subject: str, resolve: Callable[[str], str]
    ) -> dict[str, str]:
        values: dict[str, str] = {}
        for choice in self.choices:
            if choice.applies_to(rule, subject, resolve):
                values.update(
                    {
                        placeholder: resolve(value)
                        for placeholder, value in choice.values.items()
                    }
                )
        return values


def scenario_names() -> list[str]:
    return sorted(
        path.name for path in SCENARIOS.iterdir() if (path / "scenario.yaml").is_file()
    )


def load_scenario(name: str) -> Scenario:
    directory: Final = SCENARIOS / name
    document: Final = yaml.safe_load(
        (directory / "scenario.yaml").read_text(encoding="utf-8")
    )
    compose_files = [BASE_COMPOSE]
    if (directory / "compose.yml").is_file():
        compose_files.append(directory / "compose.yml")
    return Scenario(
        name=name,
        directory=directory,
        description=document["description"],
        seed=document.get("seed") or {},
        choices=tuple(
            Choice(item["rule"], item["subject"], item["values"])
            for item in document.get("choices") or []
        ),
        provisions={
            placeholder: Provision(
                placeholder=placeholder,
                gap=item["gap"],
                compose=SCENARIOS / item["compose"],
                value=item["value"],
            )
            for placeholder, item in (document.get("provisions") or {}).items()
        },
        confirm=Call(**document["confirm"]),
        fallback=Fallback(**document["fallback"]),
        drive=Call(**document["drive"]),
        expected_gaps=_expected_gaps(document.get("expected_gaps") or ()),
        compose_files=tuple(compose_files),
        environment=document.get("environment") or {},
        init_arguments=document.get("init_arguments") or "",
        templates=tuple(document.get("templates") or ()),
    )


def _expected_gaps(document: Any) -> dict[str, tuple[str, ...]]:
    if isinstance(document, Mapping):
        return {release: tuple(gaps or ()) for release, gaps in document.items()}
    return {EVERY_RELEASE: tuple(document)}
