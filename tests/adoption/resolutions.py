from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any, Final, Literal

from .findings import PrintedFinding

PLACEHOLDER: Final = re.compile(r"<(?:YOUR|A) [^<>]+>")
COMMAND_PREFIX: Final = "curl "
ALTERNATIVE_PREFIX: Final = "or "

ResolutionKind = Literal["command", "config", "none"]


class MissingChoice(Exception):
    def __init__(self, placeholder: str) -> None:
        super().__init__(f"no value is declared for {placeholder}")
        self.placeholder = placeholder


def fill(text: str, values: Mapping[str, str]) -> str:
    return PLACEHOLDER.sub(lambda match: _value(match.group(0), values), text)


def kind_of(finding: PrintedFinding) -> ResolutionKind:
    if not finding.resolution:
        return "none"
    if finding.resolution[0].startswith(COMMAND_PREFIX):
        return "command"
    return "config"


def primary_resolution(finding: PrintedFinding) -> tuple[str, ...]:
    lines: list[str] = []
    for line in finding.resolution:
        if line.startswith(ALTERNATIVE_PREFIX):
            break
        lines.append(line)
    return tuple(lines)


def merge_config(config: dict[str, Any], snippet: object) -> list[str]:
    if isinstance(snippet, list):
        return _merge_deployments(config, snippet)
    if isinstance(snippet, dict):
        _deep_merge(config, snippet)
        return []
    raise ValueError(
        f"a printed config snippet is a mapping or a model list, not {snippet!r}"
    )


def _value(placeholder: str, values: Mapping[str, str]) -> str:
    if placeholder not in values:
        raise MissingChoice(placeholder)
    return values[placeholder]


def _merge_deployments(config: dict[str, Any], items: list[Any]) -> list[str]:
    deployments: Final = config.get("model_list") or []
    unmatched: list[str] = []
    for item in items:
        name = item["model_name"]
        matching = [d for d in deployments if d.get("model_name") == name]
        if not matching:
            unmatched.append(name)
            continue
        settings = {key: value for key, value in item.items() if key != "model_name"}
        for deployment in matching:
            _deep_merge(deployment, settings)
    return unmatched


def _deep_merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            _deep_merge(existing, value)
        else:
            target[key] = copy.deepcopy(value)
