from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ruamel.yaml import YAML
from ruamel.yaml.util import load_yaml_guess_indent

MAPPING_INDENT: Final = 2
DEFAULT_SEQUENCE_INDENT: Final = 4
DEFAULT_OFFSET: Final = 2
LINE_WIDTH: Final = 4096


@dataclass
class StyledDocument:
    path: Path
    data: Any
    renderer: YAML

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8") as handle:
            self.renderer.dump(self.data, handle)


def read_styled(path: Path) -> StyledDocument:
    text: Final = path.read_text(encoding="utf-8")
    _, indent, block_seq_indent = load_yaml_guess_indent(text)
    renderer: Final = YAML()
    renderer.preserve_quotes = True
    renderer.width = LINE_WIDTH
    if block_seq_indent is None:
        renderer.indent(
            mapping=MAPPING_INDENT,
            sequence=DEFAULT_SEQUENCE_INDENT,
            offset=DEFAULT_OFFSET,
        )
    else:
        renderer.indent(
            mapping=MAPPING_INDENT, sequence=indent, offset=block_seq_indent
        )
    return StyledDocument(path, renderer.load(text), renderer)


def parse_snippet(text: str) -> Any:
    return YAML().load(text)
