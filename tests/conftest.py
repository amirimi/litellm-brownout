"""Shared fixtures.

The catalog and manifest paths are pinned to the repository copies before the
adapter is imported, so the suite never depends on the working directory it was
launched from.

``.env`` is loaded here rather than left to whichever module imports LiteLLM
first. LiteLLM calls ``load_dotenv`` at import, so the master key used to be
present only when a unit test happened to run before the end-to-end one, and
running the end-to-end file alone skipped on a missing key.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"

load_dotenv(REPO_ROOT / ".env")

os.environ.setdefault("BUDGET_POLICY_PRICES", str(CONFIG_DIR / "prices.yaml"))
os.environ.setdefault("BUDGET_POLICY_MANIFEST", str(CONFIG_DIR / "manifest.yaml"))
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from budget_policy.catalog import PriceCatalog
from budget_policy.manifest import Manifest, Posture, RoutePolicy, TeamPolicy


@pytest.fixture(autouse=True)
def isolated_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """No unit test reaches the proxy runtime.

    ``litellm.proxy.proxy_server`` builds the whole FastAPI application at
    import. ``budget_policy.runtime`` is the only place that reads it, so
    stubbing the name the adapter imported from there keeps the suite runnable
    without a gateway. Tests that care about the router substitute their own.
    """
    monkeypatch.setattr("budget_policy.adapter.router", lambda: None)


@pytest.fixture(scope="session")
def catalog() -> PriceCatalog:
    return PriceCatalog.from_file(CONFIG_DIR / "prices.yaml")


@pytest.fixture(scope="session")
def manifest() -> Manifest:
    return Manifest.from_file(CONFIG_DIR / "manifest.yaml")


@pytest.fixture
def degrading_route() -> RoutePolicy:
    return RoutePolicy(
        route="budget-lab-primary",
        fallback="budget-lab-degraded",
        degrade_at=90,
        on_counter_unavailable=Posture.BLOCK,
    )


@pytest.fixture
def strict_route() -> RoutePolicy:
    return RoutePolicy(
        route="budget-lab-strict",
        fallback=None,
        degrade_at=None,
        on_counter_unavailable=Posture.BLOCK,
    )


@pytest.fixture
def team(degrading_route: RoutePolicy, strict_route: RoutePolicy) -> TeamPolicy:
    return TeamPolicy(
        team_id="budget-lab",
        policy_epoch="test-1",
        envelope_usd=1000.0,
        budget_duration="7d",
        warn_tiers=(75, 90),
        default_posture=Posture.BLOCK,
        routes={
            degrading_route.route: degrading_route,
            strict_route.route: strict_route,
        },
    )
