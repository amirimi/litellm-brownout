from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from .api_rows import API_ROWS
from .command_rows import COMMAND_ROWS
from .config_rows import CONFIG_ROWS
from .litellm_rows import LITELLM_ROWS
from .manifest_rows import MANIFEST_ROWS
from .model import Input
from .printed_rows import PRINTED_ROWS
from .request_rows import REQUEST_ROWS

WALKED_RELEASES: Final = (
    "1.101.6",
    "1.102.4",
    "1.103.4",
    "1.104.2",
)

OPEN_FINDINGS: Final[Mapping[str, str]] = {
    "cost-map-source": (
        "check prices a model from whichever cost map LiteLLM loaded, fetched or "
        "bundled, and ignores model_info.base_model, so it can differ from the "
        "gateway's"
    ),
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
    *LITELLM_ROWS,
    *PRINTED_ROWS,
)
