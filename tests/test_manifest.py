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
        "envelope_usd": 1.0,
        "budget_duration": "7d",
        "warn_tiers": [75],
    }
    if team is not None:
        entry.update(team)
    doc: dict[str, Any] = {
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
    assert set(manifest.team_ids) == {"team-a", "team-b", "budget-lab", "kinds-lab"}


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
            envelope_usd=envelope,
            budget_duration="7d",
            warn_tiers=(75,),
            routes={},
        )


def test_a_team_cannot_be_built_with_a_window_that_does_not_tumble():
    with pytest.raises(AssertionError, match="tumble"):
        TeamPolicy(
            team_id="t",
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


def _without(doc: dict[str, Any], key: str) -> dict[str, Any]:
    return {name: value for name, value in doc.items() if name != key}


def _team_without(key: str) -> dict[str, Any]:
    doc = document()
    doc["teams"]["t"] = _without(doc["teams"]["t"], key)
    return doc


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param(["not", "a", "mapping"], id="manifest-not-a-mapping"),
        pytest.param(_without(document(), "teams"), id="teams-missing"),
        pytest.param(document(top={"teams": {}}), id="teams-empty"),
        pytest.param(document(top={"teams": ["t"]}), id="teams-not-a-mapping"),
        pytest.param(document(top={"data_class_order": []}), id="order-empty"),
        pytest.param(
            document(top={"data_class_order": "internal"}), id="order-not-a-list"
        ),
        pytest.param(
            document(top={"data_class_order": ["internal", ""]}), id="order-entry-empty"
        ),
        pytest.param(
            document(top={"data_class_order": ["internal", 3]}),
            id="order-entry-not-a-string",
        ),
        pytest.param(
            document(top={"teams": {"t": ["entry"]}}), id="team-not-a-mapping"
        ),
        pytest.param(document({"warn_tiers": 75}), id="team-tiers-not-a-list"),
        pytest.param(document({"routes": ["a"]}), id="routes-not-a-mapping"),
        pytest.param(document({"routes": {"a": ["b"]}}), id="route-not-a-mapping"),
        pytest.param(
            document({"routes": {"a": {"fallback": ""}}}), id="fallback-empty"
        ),
        pytest.param(
            document({"routes": {"a": {"fallback": 3}}}), id="fallback-not-a-string"
        ),
        pytest.param(
            document({"routes": {"a": {"warn_tiers": 75}}}), id="route-tiers-not-a-list"
        ),
        pytest.param(
            document({"routes": {"a": {"warn_tiers": ["75"]}}}),
            id="route-tier-not-an-integer",
        ),
        pytest.param(
            document({"routes": {"a": {"fallback": "b", "degrade_at": 90.5}}}),
            id="degrade-at-not-an-integer",
        ),
        pytest.param(
            document({"routes": {"a": {"fallback": "b", "degrade_at": True}}}),
            id="degrade-at-boolean",
        ),
        pytest.param(
            document(top={"teams": {123: document()["teams"]["t"]}}),
            id="team-id-a-number",
        ),
        pytest.param(
            document(top={"teams": {True: document()["teams"]["t"]}}),
            id="team-id-a-boolean",
        ),
        pytest.param(document({"routes": {7: {}}}), id="route-name-a-number"),
    ],
)
def test_a_malformed_manifest_is_refused_by_schema(doc):
    assert refused(doc).rule is RuleId.MANIFEST_SCHEMA


def test_a_quoted_number_is_a_team_id():
    loaded = load(document(top={"teams": {"0123": document()["teams"]["t"]}}))

    assert loaded.team_ids == ("0123",)


def test_tiers_listed_twice_collapse_into_one():
    loaded = load(document({"warn_tiers": [90, 75, 90]}))

    assert loaded.team_policy("t").warn_tiers == (75, 90)


def test_a_route_declared_with_no_settings_never_degrades():
    route = load(document({"routes": {"a": None}})).team_policy("t").routes["a"]

    assert (route.fallback, route.degrade_at, route.warn_tiers) == (None, None, ())


def test_a_team_without_routes_has_none():
    assert load(document({"routes": None})).team_policy("t").routes == {}


TWO_TEAMS_YAML = """
data_class_order: [internal]
teams:
  t1:
    envelope_usd: 500
    budget_duration: "7d"
    warn_tiers: [90, 75]
    routes:
      chat: {fallback: chat-small, degrade_at: 90}
  t2:
    envelope_usd: 100.0
    budget_duration: "1d"
"""

REFORMATTED_YAML = """
# the same policy, written differently
teams:
  t2: {budget_duration: "1d", envelope_usd: 100}
  t1:
    routes:
      chat:
        degrade_at: 90
        fallback: chat-small
    warn_tiers: [75, 90, 75]
    budget_duration: "7d"
    envelope_usd: 500.0
data_class_order: [internal]
"""


def from_yaml(tmp_path, name: str, text: str) -> Manifest:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return Manifest.from_file(path)


def epochs(manifest: Manifest) -> dict[str, str]:
    return {team.team_id: team.policy_epoch for team in manifest.teams}


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param(document(top={"version": "v1"}), id="version"),
        pytest.param(document({"policy_epoch": "t-v1"}), id="policy-epoch"),
    ],
)
def test_a_manifest_that_still_carries_a_bookkeeping_key_is_refused(doc):
    error = refused(doc)

    assert error.rule is RuleId.MANIFEST_SCHEMA
    assert "unknown key" in str(error)


def test_the_epoch_is_twelve_hex_characters(tmp_path):
    for epoch in epochs(from_yaml(tmp_path, "a.yaml", TWO_TEAMS_YAML)).values():
        assert len(epoch) == 12
        int(epoch, 16)


def test_key_order_formatting_comments_and_number_form_leave_the_epoch_alone(
    tmp_path,
):
    assert epochs(from_yaml(tmp_path, "a.yaml", TWO_TEAMS_YAML)) == epochs(
        from_yaml(tmp_path, "b.yaml", REFORMATTED_YAML)
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param("envelope_usd: 500", "envelope_usd: 600", id="envelope"),
        pytest.param('budget_duration: "7d"', 'budget_duration: "30d"', id="duration"),
        pytest.param("warn_tiers: [90, 75]", "warn_tiers: [90, 80]", id="team-tier"),
        pytest.param("fallback: chat-small", "fallback: chat-tiny", id="fallback"),
        pytest.param("degrade_at: 90}", "degrade_at: 85}", id="degrade-at"),
        pytest.param(
            "degrade_at: 90}", "degrade_at: 90, warn_tiers: [50]}", id="route-tier"
        ),
        pytest.param(
            "      chat: {fallback: chat-small, degrade_at: 90}",
            "      chat: {fallback: chat-small, degrade_at: 90}\n      summary: {}",
            id="route-added",
        ),
    ],
)
def test_a_policy_change_moves_that_teams_epoch_and_no_other(tmp_path, old, new):
    assert old in TWO_TEAMS_YAML
    before = epochs(from_yaml(tmp_path, "a.yaml", TWO_TEAMS_YAML))
    after = epochs(from_yaml(tmp_path, "b.yaml", TWO_TEAMS_YAML.replace(old, new)))

    assert after["t1"] != before["t1"]
    assert after["t2"] == before["t2"]


def test_the_team_id_and_the_data_class_order_leave_the_epoch_alone(tmp_path):
    before = epochs(from_yaml(tmp_path, "a.yaml", TWO_TEAMS_YAML))
    renamed = TWO_TEAMS_YAML.replace("  t2:", "  t3:").replace(
        "[internal]", "[internal, customer_pii]"
    )

    after = epochs(from_yaml(tmp_path, "b.yaml", renamed))

    assert (after["t1"], after["t3"]) == (before["t1"], before["t2"])
