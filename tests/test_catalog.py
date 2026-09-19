"""The price catalog and the fallback rules built on it.

A degradation target has to be priced, cheaper on at least one token dimension
and no dearer on either, and carry a data class at least as restrictive as the
primary's. A price that cannot support those comparisons is a load-time error.
"""

from __future__ import annotations

import pytest

from budget_policy.catalog import (
    CatalogError,
    FallbackRejected,
    ModelPrice,
    PriceCatalog,
    UnpricedModel,
    _build_price,
)


def test_a_cheaper_same_class_fallback_is_selectable(catalog: PriceCatalog):
    """The accepting case, and the lab price ordering."""
    primary = catalog.price("budget-lab-primary")
    fallback = catalog.price("budget-lab-degraded")

    assert fallback.input_cost_per_token < primary.input_cost_per_token
    assert fallback.output_cost_per_token < primary.output_cost_per_token
    assert (
        catalog.select_fallback("budget-lab-primary", "budget-lab-degraded")
        == "budget-lab-degraded"
    )


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        ("more-expensive", "more_expensive_than_primary"),
        ("same-price", "not_cheaper_than_primary"),
        ("not-in-the-catalog", "unpriced"),
        ("less-restricted", "data_class_does_not_permit_primary_traffic"),
    ],
)
def test_an_invalid_fallback_is_rejected_with_its_reason(candidate, reason):
    """Every rejection carries the reason that produced it."""
    catalog = PriceCatalog(
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

    rejection = catalog.select_fallback("primary", candidate)

    assert isinstance(rejection, FallbackRejected)
    assert rejection.reason == reason


@pytest.mark.parametrize("price", [float("nan"), float("inf"), float("-inf")])
def test_the_catalog_rejects_a_price_that_is_not_a_finite_number(price):
    """A non-finite price disables every comparison built on it.

    ``nan`` fails every ordering test, so a fallback priced ``nan`` reads as
    neither cheaper nor more expensive than its primary and slips past the
    negative-price guard. ``inf`` poisons the reservation estimate instead.
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

    Every fallback rule is a comparison between two of these, so a value that
    breaks comparison disables selection rather than failing it.
    """
    with pytest.raises(AssertionError, match=expected):
        ModelPrice(
            model="a",
            input_cost_per_token=input_cost,
            output_cost_per_token=output_cost,
            data_class="internal",
        )
