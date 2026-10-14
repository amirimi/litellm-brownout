from __future__ import annotations

from typing import Any, Final

import httpx
import pytest

from brownout.gateway_api import (
    GatewayReadError,
    LiteLLMKey,
    LiteLLMTeam,
    read_gateway,
    read_model_groups,
)
from brownout.rules import ModelGroup, is_priced

TEAMS: Final = [
    {
        "team_id": "search",
        "max_budget": 500.0,
        "budget_duration": "7d",
        "models": ["chat", "chat-cheap"],
    },
    {
        "team_id": "unbudgeted",
        "max_budget": None,
        "budget_duration": None,
        "models": [],
    },
]
KEY_PAGES: Final = [
    [
        {
            "token": "hash-a",
            "key_alias": "search-app",
            "team_id": "search",
            "max_budget": None,
            "model_max_budget": {},
            "budget_limits": None,
        },
        {
            "token": "hash-b",
            "key_alias": None,
            "team_id": None,
            "max_budget": 5.0,
            "model_max_budget": {},
            "budget_limits": None,
        },
    ],
    [
        {
            "token": "hash-c",
            "key_alias": "capped",
            "team_id": "search",
            "max_budget": None,
            "model_max_budget": {"chat": {"budget_limit": 1.0}},
            "budget_limits": [{"budget_limit": 2.0}],
        }
    ],
]
MODELS: Final = [
    {
        "model_name": "chat",
        "model_info": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6},
    },
    {
        "model_name": "chat",
        "model_info": {"input_cost_per_token": None, "output_cost_per_token": None},
    },
    {
        "model_name": "unpriced",
        "model_info": {"input_cost_per_token": 1e-6, "output_cost_per_token": None},
    },
]


def gateway_client(overrides: dict[str, Any] | None = None) -> httpx.Client:
    responses = {"/team/list": TEAMS, "/model/info": {"data": MODELS}}
    if overrides is not None:
        responses.update(overrides)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "the gateway client only reads"
        if request.url.path == "/key/list" and "/key/list" not in responses:
            page = int(request.url.params["page"])
            return httpx.Response(
                200,
                json={
                    "keys": KEY_PAGES[page - 1],
                    "total_count": 3,
                    "total_pages": len(KEY_PAGES),
                    "current_page": page,
                },
            )
        body = responses.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"error": "not found"})
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    return httpx.Client(
        base_url="http://gateway.test", transport=httpx.MockTransport(handle)
    )


def test_teams_keep_their_budget_window_and_models():
    snapshot = read_gateway(gateway_client(), lambda page, total: None)

    assert snapshot.teams == (
        LiteLLMTeam("search", 500.0, "7d", ("chat", "chat-cheap")),
        LiteLLMTeam("unbudgeted", None, None, ()),
    )


def test_every_key_page_is_read_and_own_budgets_are_named():
    pages: list[tuple[int, int]] = []

    snapshot = read_gateway(
        gateway_client(), lambda page, total: pages.append((page, total))
    )

    assert snapshot.keys == (
        LiteLLMKey("hash-a", "search-app", "search", ()),
        LiteLLMKey("hash-b", None, None, ("max_budget",)),
        LiteLLMKey("hash-c", "capped", "search", ("model_max_budget", "budget_limits")),
    )
    assert pages == [(1, 2), (2, 2)]


def test_a_model_group_is_priced_only_when_every_deployment_is():
    priced = {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}
    unpriced = {"input_cost_per_token": None, "output_cost_per_token": None}
    deployments = [
        {"model_name": "priced", "model_info": priced},
        {"model_name": "mixed", "model_info": priced},
        {"model_name": "mixed", "model_info": unpriced},
    ]

    groups = read_model_groups(gateway_client({"/model/info": {"data": deployments}}))

    assert {name: is_priced(group) for name, group in groups.items()} == {
        "priced": True,
        "mixed": False,
    }


@pytest.mark.parametrize(
    "broken",
    [
        {"/team/list": httpx.Response(401, json={"error": "bad key"})},
        {"/team/list": {"unexpected": "shape"}},
        {"/model/info": httpx.Response(500, text="boom")},
        pytest.param({"/team/list": [{"max_budget": 1.0}]}, id="team-without-id"),
        pytest.param(
            {"/team/list": [{**TEAMS[0], "team_memberships": "alice"}]},
            id="memberships-not-a-list",
        ),
        pytest.param({"/key/list": {"keys": "none"}}, id="keys-not-a-page"),
        pytest.param(
            {"/key/list": {"keys": [{"key_alias": "x"}], "total_pages": 1}},
            id="key-without-token",
        ),
        pytest.param(
            {"/model/info": {"data": ["chat"]}}, id="deployment-not-a-mapping"
        ),
    ],
)
def test_an_unreadable_gateway_raises_one_error_type(broken):
    with pytest.raises(GatewayReadError):
        read_gateway(gateway_client(broken), lambda page, total: None)


def test_a_transport_failure_raises_the_same_error_type():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(
        base_url="http://gateway.test", transport=httpx.MockTransport(refuse)
    )

    with pytest.raises(GatewayReadError, match="refused"):
        read_gateway(client, lambda page, total: None)


@pytest.mark.parametrize(
    ("prices", "priced"),
    [
        pytest.param((0, 0), False, id="zero-as-litellm-1.101.6-reports-unpriced"),
        pytest.param((0, 2e-6), True, id="free-input"),
        pytest.param((1e-6, None), False, id="one-price-missing"),
    ],
)
def test_a_model_is_priced_by_the_same_rule_the_requests_use(prices, priced):
    deployment = {
        "model_name": "mock",
        "model_info": {
            "input_cost_per_token": prices[0],
            "output_cost_per_token": prices[1],
        },
    }

    snapshot = read_gateway(
        gateway_client({"/model/info": {"data": [deployment]}}),
        lambda page, total: None,
    )

    assert is_priced(snapshot.groups["mock"]) is priced


def test_members_with_a_budget_inside_the_team_are_named():
    listed = [
        {
            **TEAMS[0],
            "team_memberships": [
                {"user_id": "alice", "litellm_budget_table": {"max_budget": 0.2}},
                {"user_id": "bob", "litellm_budget_table": None},
                {"user_id": "carol", "litellm_budget_table": {"max_budget": None}},
            ],
        }
    ]

    snapshot = read_gateway(
        gateway_client({"/team/list": listed}), lambda page, total: None
    )

    assert snapshot.teams[0].budgeted_members == ("alice",)


def test_model_groups_carry_prices_and_data_classes_from_the_gateway():
    deployments = [
        {
            "model_name": "ui-chat",
            "model_info": {
                "input_cost_per_token": 1e-6,
                "output_cost_per_token": 2e-6,
                "data_class": "internal",
            },
        },
        {
            "model_name": "ui-chat",
            "model_info": {
                "input_cost_per_token": 3e-6,
                "output_cost_per_token": 1e-6,
                "data_class": "internal",
            },
        },
        {
            "model_name": "half-priced",
            "model_info": {"input_cost_per_token": 1e-6, "output_cost_per_token": None},
        },
    ]

    groups = read_model_groups(gateway_client({"/model/info": {"data": deployments}}))

    assert groups["ui-chat"] == ModelGroup(
        "ui-chat", 3e-6, 2e-6, ("internal", "internal")
    )
    assert groups["half-priced"].output_cost_per_token is None
    assert groups["half-priced"].data_classes == (None,)


def test_the_snapshot_carries_the_same_model_groups():
    snapshot = read_gateway(gateway_client(), lambda page, total: None)

    assert snapshot.groups == read_model_groups(gateway_client())
    assert set(snapshot.groups) == {"chat", "unpriced"}
