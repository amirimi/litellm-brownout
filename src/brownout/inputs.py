from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Protocol

import yaml


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
    text: Final = read_text(path)
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise positioned(path, "is not valid YAML", _pyyaml_mark(error)) from None


def mapping(path: Path, document: object) -> Mapping[str, Any]:
    if not isinstance(document, Mapping):
        raise DocumentError(f"{path} is not a YAML mapping")
    return document


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
