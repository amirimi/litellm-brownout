from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from .api_rows import API_ROWS
from .command_rows import COMMAND_ROWS
from .compose_rows import COMPOSE_ROWS
from .config_rows import CONFIG_ROWS
from .litellm_rows import LITELLM_ROWS
from .manifest_rows import MANIFEST_ROWS
from .model import Input
from .printed_rows import PRINTED_ROWS
from .request_rows import REQUEST_ROWS

WALKED_RELEASES: Final = (
    "1.101.6",
    "1.102.4",
    "1.103.6",
    "1.104.4",
    "1.105.2",
    "1.106.1",
)

OPEN_FINDINGS: Final[Mapping[str, str]] = {
    "budgets-before-tiers": (
        "organization, tag, end-user, global proxy, budget_limits, access group, late "
        "member, default key and opted-in user budgets can refuse before the tiers, and "
        "init or check can see each of them"
    ),
}

LEDGER: Final[tuple[Input, ...]] = (
    *MANIFEST_ROWS,
    *CONFIG_ROWS,
    *API_ROWS,
    *REQUEST_ROWS,
    *COMMAND_ROWS,
    *COMPOSE_ROWS,
    *LITELLM_ROWS,
    *PRINTED_ROWS,
)
