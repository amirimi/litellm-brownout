from __future__ import annotations

from typing import Any


def router() -> Any | None:
    from litellm.proxy.proxy_server import llm_router

    return llm_router


def spend_reader() -> Any:
    from litellm.proxy.proxy_server import get_current_spend

    return get_current_spend


def spend_counter_redis() -> Any | None:
    from litellm.proxy.proxy_server import spend_counter_cache

    return spend_counter_cache.redis_cache
