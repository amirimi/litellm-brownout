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
    "name-not-a-group": (
        "LiteLLM maps a router alias, a wildcard deployment, a routing group, a team's "
        "public model name, a comma-separated model list, and before 1.97 a key's or "
        "team's alias, to deployments only after brownout's hook, so the request is "
        "served undegraded or refused with a message that blames its price"
    ),
    "mixed-price-group": (
        "LiteLLM prices a group at its highest known deployment price, so a group with "
        "an unpriced deployment passes the request-time price rule that check refuses"
    ),
    "model-info-prices": (
        "LiteLLM prices a deployment from model_info, and check reads prices only from "
        "litellm_params and the cost map"
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
    "rate-limit-fallbacks": (
        "a local rate limit retries a governed request on a router fallback, and brownout "
        "governs the retry as a new request, so the manifest's data class order does not "
        "cover the move"
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
