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
    "1.95.1",
    "1.96.2",
    "1.97.0",
    "1.98.1",
    "1.99.4",
    "1.100.5",
    "1.101.6",
    "1.102.4",
    "1.103.4",
    "1.104.2",
)

OPEN_FINDINGS: Final[Mapping[str, str]] = {
    "model-info-prices": (
        "LiteLLM bills a deployment priced in model_info, and check reads prices only "
        "from litellm_params and the cost map"
    ),
    "cost-map-source": (
        "check prices a model from whichever cost map LiteLLM loaded, fetched or "
        "bundled, so it can differ from the gateway's"
    ),
    "non-chat-pass-through": (
        "the plugin sees every call type and governs only chat completions, so other "
        "calls from governed teams pass at full price"
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
