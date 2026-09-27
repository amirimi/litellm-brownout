"""The price catalog and the fallback rules built on it.

A degradation target has to be priced, differ from its route, and carry a data
class at least as restrictive as the route's. Its price relative to the route is
the route owner's call, approved in the manifest. A price that cannot stand in
for what LiteLLM reserves against is a load-time error.
"""

from __future__ import annotations

import pytest

from budget_policy.catalog import (
    CatalogError,
    ModelPrice,
    PriceCatalog,
    UnpricedModel,
    _build_price,
)


def _pairing_catalog() -> PriceCatalog:
    return PriceCatalog(
        version="test",
        data_class_order=("internal", "customer_pii"),
        prices={
            "primary": ModelPrice("primary", 0.001, 0.002, "customer_pii"),
            "more-expensive": ModelPrice(
                "more-expensive", 0.001, 0.003, "customer_pii"
            ),
            "same-price": ModelPrice("same-price", 0.001, 0.002, "customer_pii"),
            "less-restricted": ModelPrice(
                "less-restricted", 0.0001, 0.0002, "internal"
            ),
        },
    )


def test_the_lab_pairing_is_accepted(catalog: PriceCatalog):
    catalog.require_fallback("budget-lab-primary", "budget-lab-degraded")


@pytest.mark.parametrize("candidate", ["more-expensive", "same-price"])
def test_a_fallback_at_or_above_the_routes_price_is_the_owners_call(candidate):
    """The manifest owner approved the pairing, and price is theirs to judge."""
    _pairing_catalog().require_fallback("primary", candidate)


@pytest.mark.parametrize(
    ("primary", "candidate", "problem"),
    [
        ("primary", "primary", "itself"),
        ("primary", "not-in-the-catalog", "no committed price"),
        ("not-in-the-catalog", "same-price", "no committed price"),
        ("primary", "less-restricted", "data class"),
    ],
)
def test_a_pairing_the_platform_cannot_allow_is_a_catalog_error(
    primary, candidate, problem
):
    """Each is a fact about two loaded files, so it is refused before any traffic."""
    with pytest.raises(CatalogError, match=problem):
        _pairing_catalog().require_fallback(primary, candidate)


@pytest.mark.parametrize("price", [float("nan"), float("inf"), float("-inf")])
def test_the_catalog_rejects_a_price_that_is_not_a_finite_number(price):
    """A non-finite price cannot match the price LiteLLM reserves against.

    ``nan`` never equals itself, so the contract that the catalog equals every
    deployment's price could not hold, and it slips past the negative-price
    guard. ``inf`` poisons the reservation estimate.
    """
    with pytest.raises(CatalogError, match="finite"):
        _build_price(
            path="test",
            name="broken",
            entry={
                "input_cost_per_token": price,
                "output_cost_per_token": 0.1,
                "data_class": "internal",
            },
            data_class_order=("internal",),
        )


def test_the_catalog_rejects_a_route_that_is_free_on_both_dimensions():
    """A route priced at zero everywhere cannot be enforced.

    Spend never moves, so no threshold is ever reached and the envelope means
    nothing for that route. That is a catalog error, not a discount.
    """
    with pytest.raises(CatalogError, match="free"):
        _build_price(
            path="test",
            name="free-route",
            entry={
                "input_cost_per_token": 0.0,
                "output_cost_per_token": 0.0,
                "data_class": "internal",
            },
            data_class_order=("internal",),
        )


def test_an_unknown_data_class_is_a_catalog_error():
    catalog = PriceCatalog(
        version="test",
        data_class_order=("internal",),
        prices={"a": ModelPrice("a", 0.1, 0.1, "internal")},
    )
    with pytest.raises(CatalogError):
        catalog.sensitivity("nonexistent")


def test_an_unpriced_lookup_names_the_model():
    catalog = PriceCatalog(version="test", data_class_order=("internal",), prices={})
    with pytest.raises(UnpricedModel) as raised:
        catalog.price("ghost")
    assert raised.value.model == "ghost"


@pytest.mark.parametrize(
    ("input_cost", "output_cost", "expected"),
    [
        (float("nan"), 0.001, "finite"),
        (0.0001, float("inf"), "finite"),
        (-0.0001, 0.001, "negative"),
        (0.0, 0.0, "free"),
    ],
)
def test_a_price_cannot_be_constructed_in_a_shape_comparisons_cannot_use(
    input_cost, output_cost, expected
):
    """The loader reports these as catalog errors. The type refuses them outright.

    The catalog stands in for the prices LiteLLM reserves against, so a value
    that no deployment could carry is refused rather than compared.
    """
    with pytest.raises(AssertionError, match=expected):
        ModelPrice(
            model="a",
            input_cost_per_token=input_cost,
            output_cost_per_token=output_cost,
            data_class="internal",
        )
