from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from ..support import REPO_ROOT

README: Final = REPO_ROOT / "README.md"
MARKER: Final = re.compile(r"^<!-- adoption: (?P<name>[a-z-]+) -->$")
FENCE: Final = "```"


def readme_blocks(text: str) -> dict[str, str]:
    lines: Final = text.splitlines()
    blocks: dict[str, str] = {}
    for index, line in enumerate(lines):
        marker = MARKER.match(line)
        if marker is not None:
            blocks[marker["name"]] = _fenced_block(lines, index + 1, marker["name"])
    return blocks


def adapt(block: str, replacements: Sequence[tuple[str, str]]) -> str:
    adapted = block
    for original, replacement in replacements:
        if original not in adapted:
            raise ValueError(f"the README block no longer contains {original!r}")
        adapted = adapted.replace(original, replacement)
    return adapted


def _fenced_block(lines: Sequence[str], start: int, name: str) -> str:
    if start >= len(lines) or not lines[start].startswith(FENCE):
        raise ValueError(f"<!-- adoption: {name} --> is not followed by a fenced block")
    end = start + 1
    while end < len(lines) and lines[end] != FENCE:
        end += 1
    if end == len(lines):
        raise ValueError(f"the block after <!-- adoption: {name} --> never closes")
    return "\n".join(lines[start + 1 : end]) + "\n"
