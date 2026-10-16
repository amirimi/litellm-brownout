from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from brownout.refusals import RefusalReason, RuleId

Source = Literal["brownout", "litellm", "release", "printed"]
Stage = Literal["check", "init", "start", "request"]

FIRST_MINOR: Final = 101
LAST_MINOR: Final = 105


@dataclass(frozen=True)
class Supported:
    tests: tuple[str, ...]


@dataclass(frozen=True)
class Refused:
    stage: Stage
    rule: RuleId | None
    tests: tuple[str, ...]
    reason: RefusalReason | None = None


@dataclass(frozen=True)
class Documented:
    unseen_because: str
    docs: str
    phrase: str


@dataclass(frozen=True)
class Open:
    finding: str


Disposition = Supported | Refused | Documented | Open


@dataclass(frozen=True)
class Form:
    form: str
    becomes: str
    disposition: Disposition


@dataclass(frozen=True)
class Input:
    shape: str
    source: Source
    parser: str | None
    reads: tuple[str, ...]
    cites: tuple[str, ...]
    forms: tuple[Form, ...]
