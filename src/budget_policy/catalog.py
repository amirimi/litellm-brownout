"""Committed price catalog for every route the gateway serves.

LiteLLM's reservation path returns ``None`` for a model it cannot price and then
skips reservation entirely, which turns an unknown price into a silent
enforcement hole. This catalog is the component's own pricing authority. It
raises rather than returning ``None``, so an unpriced model is a platform
incident and never an admission.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any, Final

import yaml


class CatalogError(Exception):
    """The catalog file itself is unusable."""


class UnpricedModel(Exception):
    """No committed price exists for a model the gateway was asked to serve.

    Never a legitimate state. No model is free, so this is a gap in the catalog
    and the response to not knowing a price is to stop admitting the request.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"No committed price for model {model!r}")


@dataclass(frozen=True)
class ModelPrice:
    """A committed per-token price, in a shape every comparison can rely on.

    A ``NaN`` price compares false against every threshold, so a fallback
    carrying one reads as neither cheaper nor more expensive than its primary. A
    route free on both dimensions can never move spend, so no threshold on it is
    reachable. The loader reports either as a catalog error and the assertions
    catch any construction that bypasses it.
    """

    model: str
    input_cost_per_token: float
    output_cost_per_token: float
    data_class: str

    def __post_init__(self) -> None:
        assert self.model, "a price needs a model name"
        assert self.data_class, f"model {self.model!r} needs a data class"
        assert isfinite(self.input_cost_per_token) and isfinite(
            self.output_cost_per_token
        ), f"model {self.model!r} has a price that is not a finite number"
        assert self.input_cost_per_token >= 0 and self.output_cost_per_token >= 0, (
            f"model {self.model!r} has a negative price"
        )
        assert self.input_cost_per_token > 0 or self.output_cost_per_token > 0, (
            f"model {self.model!r} is free on both token dimensions"
        )


@dataclass(frozen=True)
class FallbackRejected:
    candidate: str
    reason: str


class PriceCatalog:
    """Per-token prices and data classes, keyed by LiteLLM model group name."""

    def __init__(
        self,
        version: str,
        prices: Mapping[str, ModelPrice],
        data_class_order: tuple[str, ...],
    ) -> None:
        self.version: Final = version
        self._prices: Final = dict(prices)
        self._data_class_order: Final = data_class_order

    @classmethod
    def from_file(cls, path: str | Path) -> PriceCatalog:
        document = _load_yaml(path)
        version = document.get("version")
        if not isinstance(version, str) or not version:
            raise CatalogError(f"{path}: a catalog must carry a string 'version'")

        order = document.get("data_class_order")
        if not isinstance(order, list) or not order:
            raise CatalogError(
                f"{path}: 'data_class_order' must list data classes, least sensitive first"
            )
        data_class_order = tuple(str(entry) for entry in order)

        models = document.get("models")
        if not isinstance(models, dict) or not models:
            raise CatalogError(f"{path}: 'models' must be a non-empty mapping")

        prices = {
            name: _build_price(path, name, entry, data_class_order)
            for name, entry in models.items()
        }
        return cls(version=version, prices=prices, data_class_order=data_class_order)

    def price(self, model: str) -> ModelPrice:
        try:
            return self._prices[model]
        except KeyError:
            raise UnpricedModel(model) from None

    def sensitivity(self, data_class: str) -> int:
        try:
            return self._data_class_order.index(data_class)
        except ValueError:
            raise CatalogError(f"Unknown data class {data_class!r}") from None

    def select_fallback(
        self, primary: str, candidate: str | None
    ) -> str | FallbackRejected | None:
        """Validate a degradation target before it is allowed to serve traffic.

        A target is selectable only when it is priced, costs no more on either
        token dimension and less on at least one, and carries a data class at
        least as restrictive as the primary's. Returns the target, a rejection
        carrying the reason, or None when the route declares no target at all.
        """
        if candidate is None:
            return None
        if candidate == primary:
            return FallbackRejected(candidate, "same_route_as_primary")

        primary_price = self.price(primary)
        try:
            candidate_price = self.price(candidate)
        except UnpricedModel:
            return FallbackRejected(candidate, "unpriced")

        if (
            candidate_price.input_cost_per_token > primary_price.input_cost_per_token
            or candidate_price.output_cost_per_token
            > primary_price.output_cost_per_token
        ):
            return FallbackRejected(candidate, "more_expensive_than_primary")

        if (
            candidate_price.input_cost_per_token == primary_price.input_cost_per_token
            and candidate_price.output_cost_per_token
            == primary_price.output_cost_per_token
        ):
            return FallbackRejected(candidate, "not_cheaper_than_primary")

        if self.sensitivity(candidate_price.data_class) < self.sensitivity(
            primary_price.data_class
        ):
            return FallbackRejected(
                candidate, "data_class_does_not_permit_primary_traffic"
            )

        return candidate


def _build_price(
    path: str | Path,
    name: str,
    entry: Any,
    data_class_order: tuple[str, ...],
) -> ModelPrice:
    if not isinstance(entry, dict):
        raise CatalogError(f"{path}: model {name!r} must be a mapping")
    try:
        input_cost = float(entry["input_cost_per_token"])
        output_cost = float(entry["output_cost_per_token"])
        data_class = str(entry["data_class"])
    except KeyError as exc:
        raise CatalogError(
            f"{path}: model {name!r} is missing {exc.args[0]!r}"
        ) from None
    except (TypeError, ValueError) as exc:
        raise CatalogError(
            f"{path}: model {name!r} has a non-numeric price: {exc}"
        ) from None

    if not isfinite(input_cost) or not isfinite(output_cost):
        raise CatalogError(
            f"{path}: model {name!r} has a price that is not a finite number. "
            "A NaN price compares false against every threshold and a fallback "
            "carrying one reads as neither cheaper nor more expensive"
        )
    if input_cost < 0 or output_cost < 0:
        raise CatalogError(f"{path}: model {name!r} has a negative price")
    if input_cost == 0 and output_cost == 0:
        raise CatalogError(
            f"{path}: model {name!r} is free on both token dimensions, so spend "
            "on it never moves and no threshold can be reached"
        )
    if data_class not in data_class_order:
        raise CatalogError(
            f"{path}: model {name!r} declares data class {data_class!r} outside 'data_class_order'"
        )

    return ModelPrice(
        model=name,
        input_cost_per_token=input_cost,
        output_cost_per_token=output_cost,
        data_class=data_class,
    )


def _load_yaml(path: str | Path) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except FileNotFoundError:
        raise CatalogError(f"{path}: catalog file not found") from None
    if not isinstance(document, dict):
        raise CatalogError(f"{path}: catalog must be a YAML mapping")
    return document
