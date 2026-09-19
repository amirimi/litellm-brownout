"""Everything that must not be imported until the gateway is running.

Importing ``litellm.proxy.proxy_server`` builds the whole FastAPI application.
A module that names it at the top cannot be unit tested and cannot be imported
by anything that is not the running proxy, so these three reads are deferred.
Each is a single-purpose function whose entire body is the import, which is the
one shape a test can replace wholesale.

Nothing else in this package defers an import.
"""

from __future__ import annotations

from typing import Any


def router() -> Any | None:
    """The configured Router, or None outside the proxy runtime."""
    from litellm.proxy.proxy_server import llm_router

    return llm_router


def spend_reader() -> Any:
    """``get_current_spend``, LiteLLM's read of a spend counter."""
    from litellm.proxy.proxy_server import get_current_spend

    return get_current_spend


def spend_counter_redis() -> Any | None:
    """The Redis the spend counter already depends on, or None when unset."""
    from litellm.proxy.proxy_server import spend_counter_cache

    return spend_counter_cache.redis_cache
