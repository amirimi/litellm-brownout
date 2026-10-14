from __future__ import annotations

from typing import Any

import pytest

from brownout.manifest import (
    WINDOW_SECONDS,
    Manifest,
    ManifestError,
    RoutePolicy,
    TeamPolicy,
)
from brownout.refusals import RuleId


def document(
    team: dict[str, Any] | None = None, top: dict[str, Any] | None = None
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "policy_epoch": "test-1",
        "envelope_usd": 1.0,
        "budget_duration": "7d",
        "warn_tiers": [75],
    }
    if team is not None:
        entry.update(team)
    doc: dict[str, Any] = {
        "version": "v1",
        "data_class_order": ["internal"],
        "teams": {"t": entry},
    }
    if top is not None:
        doc.update(top)
    return doc


def load(doc: dict[str, Any]) -> Manifest:
    return Manifest.from_document("test.yaml", doc)


def refused(doc: dict[str, Any]) -> ManifestError:
    with pytest.raises(ManifestError) as raised:
        load(doc)
    return raised.value


def test_the_lab_manifest_loads(manifest):
    assert manifest.data_class_order == ("internal", "customer_pii")
    assert set(manifest.team_ids) == {"team-a", "team-b", "budget-lab"}


@pytest.mark.parametrize("tier", [0, 100, 150, True, 75.5, "75"])
def test_a_team_tier_outside_the_range_it_owns_is_refused(tier):
    assert refused(document({"warn_tiers": [tier]})).rule is RuleId.MANIFEST_SCHEMA


@pytest.mark.parametrize("tier", [0, 100])
def test_a_route_tier_outside_the_range_it_owns_is_refused(tier):
    error = refused(document({"routes": {"a": {"warn_tiers": [tier]}}}))

    assert error.rule is RuleId.MANIFEST_SCHEMA


def test_team_and_route_tiers_are_optional():
    loaded = load(document({"warn_tiers": None, "routes": {"a": {}}}))
    team = loaded.team_policy("t")

    assert team is not None
    assert team.warn_tiers == ()
    assert team.routes["a"].warn_tiers == ()


def test_a_degrade_threshold_at_one_hundred_percent_is_refused():
    error = refused(document({"routes": {"a": {"fallback": "b", "degrade_at": 100}}}))

    assert error.rule is RuleId.MANIFEST_SCHEMA


def test_a_degrade_threshold_with_no_fallback_is_refused():
    error = refused(document({"routes": {"a": {"degrade_at": 90}}}))

    assert error.rule is RuleId.MANIFEST_SCHEMA


def test_a_route_that_falls_back_to_itself_is_refused():
    error = refused(document({"routes": {"a": {"fallback": "a", "degrade_at": 90}}}))

    assert error.rule is RuleId.FALLBACK_DISTINCT


@pytest.mark.parametrize("duration", sorted(WINDOW_SECONDS))
def test_every_window_that_tumbles_is_accepted(duration):
    loaded = load(document({"budget_duration": duration}))

    assert loaded.team_policy("t").budget_duration == duration


@pytest.mark.parametrize(
    "duration",
    [
        "2d",
        "3d",
        "14d",
        "31d",
        "2w",
        "5h",
        "48h",
        "90m",
        "30m",
        "1m",
        "30s",
        "2mo",
        "7dx",
        "7D",
        "7",
        "0d",
        "-1d",
        "1y",
        "banana",
        7,
    ],
)
def test_a_window_that_does_not_tumble_is_refused(duration):
    error = refused(document({"budget_duration": duration}))

    assert error.rule is RuleId.DURATION_TUMBLES
    assert "budget_duration" in str(error)


@pytest.mark.parametrize(
    ("doc", "key"),
    [
        (document(top={"unexpected": True}), "unexpected"),
        (document({"on_counter_unavailable": "block"}), "on_counter_unavailable"),
        (document({"routes": {"a": {"degrade_At": 90}}}), "degrade_At"),
    ],
)
def test_an_unknown_key_is_refused_by_name(doc, key):
    error = refused(doc)

    assert error.rule is RuleId.MANIFEST_SCHEMA
    assert repr(key) in str(error)


def test_a_fallback_requires_a_data_class_order():
    doc = document({"routes": {"a": {"fallback": "b", "degrade_at": 90}}})
    del doc["data_class_order"]

    assert "data_class_order" in str(refused(doc))


def test_a_manifest_without_fallbacks_needs_no_data_class_order():
    doc = document()
    del doc["data_class_order"]

    assert load(doc).data_class_order == ()


def test_a_data_class_listed_twice_is_refused():
    assert "twice" in str(
        refused(document(top={"data_class_order": ["internal", "internal"]}))
    )


@pytest.mark.parametrize("envelope", [0, -1.0, float("nan"), float("inf"), "500", True])
def test_an_unusable_envelope_is_refused(envelope):
    assert refused(document({"envelope_usd": envelope})).rule is RuleId.MANIFEST_SCHEMA


def test_an_undeclared_route_gets_a_policy_that_never_degrades(team):
    policy = team.route_policy("never-declared")

    assert (policy.fallback, policy.degrade_at, policy.warn_tiers) == (None, None, ())


def test_a_route_cannot_be_built_with_a_threshold_it_cannot_act_on():
    with pytest.raises(AssertionError, match="no fallback"):
        RoutePolicy(route="a", fallback=None, degrade_at=90, warn_tiers=())


@pytest.mark.parametrize("envelope", [0.0, -1.0, float("nan"), float("inf")])
def test_a_team_cannot_be_built_with_an_unusable_envelope(envelope):
    with pytest.raises(AssertionError, match="envelope"):
        TeamPolicy(
            team_id="t",
            policy_epoch="test-1",
            envelope_usd=envelope,
            budget_duration="7d",
            warn_tiers=(75,),
            routes={},
        )


def test_a_team_cannot_be_built_with_a_window_that_does_not_tumble():
    with pytest.raises(AssertionError, match="tumble"):
        TeamPolicy(
            team_id="t",
            policy_epoch="test-1",
            envelope_usd=1.0,
            budget_duration="2d",
            warn_tiers=(75,),
            routes={},
        )


@pytest.mark.parametrize(
    "routes",
    [
        pytest.param({"openai/*": {}}, id="wildcard-route"),
        pytest.param(
            {"chat": {"fallback": "openai/*", "degrade_at": 90}}, id="wildcard-fallback"
        ),
    ],
)
def test_a_wildcard_group_cannot_be_a_route_or_a_fallback(routes):
    error = refused(document({"routes": routes}))

    assert error.rule is RuleId.MANIFEST_SCHEMA
    assert "concrete model group" in str(error)
