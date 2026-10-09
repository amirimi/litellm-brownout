from __future__ import annotations

import os

from .datadog import DatadogSink

DEFAULT_MANIFEST_PATH = "config/manifest.yaml"


def manifest_path() -> str:
    return os.getenv("BROWNOUT_MANIFEST", DEFAULT_MANIFEST_PATH)


def webhook_url() -> str | None:
    return os.getenv("BROWNOUT_WEBHOOK_URL")


def datadog_sink() -> DatadogSink | None:
    api_key = os.getenv("DD_API_KEY")
    if not api_key:
        return None
    return DatadogSink(
        api_key=api_key,
        site=os.getenv("DD_SITE", "datadoghq.com"),
        env=os.getenv("DD_ENV", "unknown"),
    )
