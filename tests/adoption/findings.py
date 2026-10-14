from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

FINDING_LINE: Final = re.compile(
    r"^(?P<rule>[a-z-]+)\t(?P<subject>[^\t]+)\t(?P<detail>.*)$"
)
RESOLUTION_INDENT: Final = "    "
TRUNCATED_SUBJECT: Final = "..."


@dataclass(frozen=True)
class PrintedFinding:
    rule: str
    subject: str
    detail: str
    resolution: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str]:
        return (self.rule, self.subject)

    @property
    def truncated(self) -> bool:
        return self.subject == TRUNCATED_SUBJECT


def parse_findings(output: str) -> tuple[PrintedFinding, ...]:
    findings: list[PrintedFinding] = []
    header: re.Match[str] | None = None
    resolution: list[str] = []
    for line in output.splitlines():
        if header is not None and line.startswith(RESOLUTION_INDENT):
            resolution.append(line.removeprefix(RESOLUTION_INDENT))
            continue
        if header is not None:
            findings.append(_finding(header, resolution))
            resolution = []
        header = FINDING_LINE.match(line)
    if header is not None:
        findings.append(_finding(header, resolution))
    return tuple(findings)


def _finding(header: re.Match[str], resolution: list[str]) -> PrintedFinding:
    return PrintedFinding(
        rule=header["rule"],
        subject=header["subject"],
        detail=header["detail"],
        resolution=tuple(resolution),
    )
