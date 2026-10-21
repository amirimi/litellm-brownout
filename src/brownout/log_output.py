from __future__ import annotations

import logging
import sys
from typing import Final

LITELLM_PROXY_LOGGER: Final = "LiteLLM Proxy"


class LiteLLMLogHandlerMissing(Exception):
    pass


class LiteLLMFormattedHandler(logging.StreamHandler):
    def __init__(self, source: logging.Handler) -> None:
        super().__init__()
        self._source: Final = source

    def format(self, record: logging.LogRecord) -> str:
        return self._source.format(record)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            self.stream = sys.stderr
        else:
            self.stream = sys.stdout
        super().emit(record)


def install(target: logging.Logger, litellm_logger: logging.Logger) -> None:
    if not litellm_logger.handlers:
        raise LiteLLMLogHandlerMissing(
            f"LiteLLM's {litellm_logger.name!r} logger has no handler to take the log format from"
        )
    for existing in list(target.handlers):
        if isinstance(existing, LiteLLMFormattedHandler):
            target.removeHandler(existing)
    target.addHandler(LiteLLMFormattedHandler(litellm_logger.handlers[0]))
    target.setLevel(logging.INFO)
