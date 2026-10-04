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
