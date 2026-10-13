from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from types import ModuleType
from typing import Any, Final, assert_never

from .inputs import Mark, positioned
from .onboarding import (
    AppendSetting,
    ConfigChange,
    RemoveSetting,
    SetSetting,
    TeamEntry,
)

MANIFEST_VERSION: Final = "v1"
LINE_WIDTH: Final = 4096
DEFAULT_INDENT: Final = 2
DEFAULT_SEQUENCE_INDENT: Final = 4


@dataclass
class YamlDocument:
    data: Any
    renderer: Any


def yaml_runtime() -> ModuleType:
    import ruamel.yaml.util

    return ruamel.yaml


def load_document(source: Path, text: str) -> YamlDocument:
    runtime: Final = yaml_runtime()
    try:
        return _parse(runtime, text)
    except runtime.YAMLError as error:
        raise positioned(
            source, _problem(runtime, error), _mark(runtime, error)
        ) from None


def _parse(runtime: ModuleType, text: str) -> YamlDocument:
    _, sequence_indent, dash_offset = runtime.util.load_yaml_guess_indent(text)
    renderer: Final = runtime.YAML()
    renderer.preserve_quotes = True
    renderer.width = LINE_WIDTH
    renderer.indent(
        mapping=dash_offset or DEFAULT_INDENT,
        sequence=sequence_indent or DEFAULT_SEQUENCE_INDENT,
        offset=dash_offset or DEFAULT_INDENT,
    )
    return YamlDocument(data=renderer.load(text), renderer=renderer)


def dump_document(document: YamlDocument) -> str:
    stream: Final = StringIO()
    document.renderer.dump(document.data, stream)
    return stream.getvalue()


def apply_config_changes(
    document: YamlDocument, changes: Iterable[ConfigChange]
) -> None:
    for change in changes:
        section = _section(document, change.section)
        match change:
            case SetSetting(key=key, value=value):
                section[key] = value
            case RemoveSetting(key=key):
                section.pop(key, None)
            case AppendSetting(key=key, value=value):
                _append(section, key, value)
            case _:
                assert_never(change)


def append_teams(document: YamlDocument, entries: Iterable[TeamEntry]) -> None:
    teams: Final = document.data["teams"]
    for entry in entries:
        assert entry.team_id not in teams, f"team {entry.team_id!r} already present"
        teams[entry.team_id] = entry.document()


def new_manifest_document(entries: Iterable[TeamEntry]) -> YamlDocument:
    runtime: Final = yaml_runtime()
    document: Final = _parse(runtime, "")
    document.data = runtime.comments.CommentedMap()
    document.data["version"] = MANIFEST_VERSION
    document.data["teams"] = {entry.team_id: entry.document() for entry in entries}
    return _parse(runtime, dump_document(document))


def to_plain(document: YamlDocument | Any) -> Any:
    if isinstance(document, YamlDocument):
        return _plain(document.data)
    return _plain(document)


def unified_diff(before: str, after: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=name,
            tofile=name,
        )
    )


def _problem(runtime: ModuleType, error: Exception) -> str:
    if isinstance(error, runtime.constructor.DuplicateKeyError):
        return "has a duplicate key"
    return "is not valid YAML"


def _mark(runtime: ModuleType, error: Exception) -> Mark | None:
    if not isinstance(error, runtime.error.MarkedYAMLError):
        return None
    if error.problem_mark is not None:
        return error.problem_mark
    return error.context_mark


def _section(document: YamlDocument, name: str) -> Any:
    section = document.data.get(name)
    if section is None:
        document.data[name] = yaml_runtime().comments.CommentedMap()
        section = document.data[name]
    return section


def _append(section: Any, key: str, value: str) -> None:
    current = section.get(key)
    if current is None:
        section[key] = [value]
    elif isinstance(current, list):
        current.append(value)
    else:
        section[key] = [current, value]


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    return value
