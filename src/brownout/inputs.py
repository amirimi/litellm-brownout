from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Protocol

import yaml

INCLUDE_KEY: Final = "include"


class DocumentError(Exception):
    pass


class Mark(Protocol):
    line: int
    column: int


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DocumentError(f"{path} was not found") from None
    except UnicodeDecodeError as error:
        raise DocumentError(f"{path} is not UTF-8 text at byte {error.start}") from None
    except OSError as error:
        raise DocumentError(f"{path} could not be read ({error.strerror})") from None


def load_yaml(path: Path) -> object:
    return load_yaml_text(path, read_text(path))


def load_yaml_text(path: Path, text: str) -> object:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise positioned(path, "is not valid YAML", _pyyaml_mark(error)) from None


def mapping(path: Path, document: object) -> Mapping[str, Any]:
    if not isinstance(document, Mapping):
        raise DocumentError(f"{path} is not a YAML mapping")
    return document


def with_includes(path: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    merged: Final = {key: value for key, value in config.items() if key != INCLUDE_KEY}
    for included_path in _included_paths(path, config.get(INCLUDE_KEY)):
        included = mapping(included_path, load_yaml(included_path))
        if INCLUDE_KEY in included:
            raise DocumentError(
                f"{included_path} includes more files, and LiteLLM resolves nested "
                f"includes differently across releases. List every file in {path}"
            )
        for key, value in included.items():
            existing = merged.get(key)
            if isinstance(value, list) and isinstance(existing, list):
                merged[key] = [*existing, *value]
            else:
                merged[key] = value
    return merged


def _included_paths(path: Path, includes: object) -> list[Path]:
    if includes is None:
        return []
    if not isinstance(includes, list) or not all(
        isinstance(name, str) for name in includes
    ):
        raise DocumentError(f"{path} include must list files")
    paths: Final = [path.parent / name for name in includes]
    resolved: Final = [included.resolve() for included in paths]
    for index, included in enumerate(resolved):
        if included in resolved[:index]:
            raise DocumentError(f"{path} includes {includes[index]} twice")
    return paths


def positioned(path: Path, problem: str, mark: Mark | None) -> DocumentError:
    if mark is None:
        return DocumentError(f"{path} {problem}")
    return DocumentError(
        f"{path} {problem} at line {mark.line + 1}, column {mark.column + 1}"
    )


def _pyyaml_mark(error: yaml.YAMLError) -> Mark | None:
    if not isinstance(error, yaml.MarkedYAMLError):
        return None
    if error.problem_mark is not None:
        return error.problem_mark
    return error.context_mark
