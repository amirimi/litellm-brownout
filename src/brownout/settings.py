from __future__ import annotations

import os

DEFAULT_MANIFEST_PATH = "config/manifest.yaml"


def manifest_path() -> str:
    return os.getenv("BROWNOUT_MANIFEST", DEFAULT_MANIFEST_PATH)


def master_key() -> str | None:
    return os.getenv("LITELLM_MASTER_KEY")
