# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "httpx>=0.28,<0.29",
# ]
# ///

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import httpx


from storefront.core import DetectedStore, Http, ToolError, parse_item_ref, validate_ref
from storefront.adapters import woocommerce


def detection() -> DetectedStore:
    return DetectedStore(
        origin="https://store.example",
        entry_url="https://store.example/",
        platform="woocommerce",
        api_origin="https://store.example",
        evidence=("Store API cart totals",),
    )


def totals() -> dict[str, object]:
    return {
        "total_items": "595",
        "total_items_tax": "0",
        "total_discount": "0",
        "total_discount_tax": "0",
        "total_shipping": "695",
        "total_shipping_tax": "0",
        "total_price": "1290",
        "total_tax": "0",
        "currency_code": "USD",
        "currency_minor_unit": 2,
    }


class WooCommerceTests(unittest.TestCase):
    def test_search_normalizes_product_and_stable_reference(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 19251,
                        "name": "Soil Moisture Sensor",
                        "type": "simple",
                        "sku": "MOD-160",
                        "is_in_stock": True,
                        "is_purchasable": True,
                        "permalink": "https://store.example/product/sensor/",
                        "prices": {
                            "price": "595",
                            "currency_code": "USD",
                            "currency_minor_unit": 2,
                        },
                        "add_to_cart": {"minimum": 1},
                    }
                ],
                request=request,
            )

        result = woocommerce.search(
            Http(httpx.MockTransport(handler)), detection(), "sensor"
        )
        self.assertEqual(
            result["items"][0]["price"], {"amount": "5.95", "currency": "USD"}
        )
        self.assertEqual(
            parse_item_ref(result["items"][0]["item_ref"], "woocommerce"),
            {"minimum": 1, "product_id": 19251, "product_type": "simple"},
        )

    def test_search_rejects_boolean_integer_fields(self) -> None:
        product = {
            "id": 19251,
            "name": "Soil Moisture Sensor",
            "type": "simple",
            "sku": "MOD-160",
            "is_in_stock": True,
            "is_purchasable": True,
            "permalink": "https://store.example/product/sensor/",
            "prices": {
                "price": "595",
                "currency_code": "USD",
                "currency_minor_unit": 2,
            },
            "add_to_cart": {"minimum": 1},
        }
        cases = [
            ("id", True),
            ("minimum", True),
            ("currency_minor_unit", True),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                invalid = json.loads(json.dumps(product))
                if field == "minimum":
                    invalid["add_to_cart"][field] = value
                elif field == "currency_minor_unit":
                    invalid["prices"][field] = value
                else:
                    invalid[field] = value
                http = Http(
                    httpx.MockTransport(
                        lambda request, invalid=invalid: httpx.Response(
                            200, json=[invalid], request=request
                        )
                    )
                )
                with http.client, self.assertRaises(ToolError):
                    woocommerce.search(http, detection(), "sensor")

    def test_quote_rejects_boolean_integer_reference_fields(self) -> None:
        cases = [
            {"product_id": True, "product_type": "simple", "minimum": 1},
            {"product_id": 19251, "product_type": "simple", "minimum": True},
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ToolError):
                validate_ref(
                    {
                        "platform": "woocommerce",
                        "store": "https://store.example",
                        **payload,
                    }
                )

    def test_quote_rejects_extra_reference_key_before_http(self) -> None:
        with self.assertRaisesRegex(ToolError, "unknown keys"):
            validate_ref(
                {
                    "platform": "woocommerce",
                    "store": "https://store.example",
                    "product_id": 19251,
                    "product_type": "simple",
                    "minimum": 1,
                    "extra": "forged",
                }
            )

    def test_selected_cart_item_rejects_boolean_product_id(self) -> None:
        cart = {"items": [{"id": True, "key": "wrong-item"}]}
        with self.assertRaisesRegex(ToolError, "exact selected product"):
            woocommerce._selected_item_key(cart, 1)

    def test_quote_reports_cleanup_failure_instead_of_leaving_partial_state(self) -> None:
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(
                    200,
                    headers={"Cart-Token": "secret"},
                    json={"totals": totals()},
                    request=request,
                )
            if request.url.path.endswith("/add-item"):
                self.assertEqual(request.headers["Cart-Token"], "secret")
                self.assertEqual(
                    json.loads(request.content), {"id": 19251, "quantity": 1}
                )
                return httpx.Response(
                    200,
                    json={
                        "items": [{"id": 19251, "key": "item-secret"}],
                        "totals": totals(),
                    },
                    request=request,
                )
            if request.url.path.endswith("/update-customer"):
                return httpx.Response(
                    200,
                    json={
                        "totals": {**totals(), "total_tax": "293"},
                        "shipping_rates": [
                            {
                                "shipping_rates": [
                                    {
                                        "rate_id": "wf_shipping_ups_fallback",
                                        "name": "UPS Flat Rate",
                                        "price": "1995",
                                        "taxes": "125",
                                        "selected": True,
                                        "currency_code": "USD",
                                        "currency_minor_unit": 2,
                                    },
                                    {
                                        "rate_id": "local_pickup:1",
                                        "name": "Store pickup",
                                        "price": "0",
                                        "taxes": "0",
                                        "selected": False,
                                        "currency_code": "USD",
                                        "currency_minor_unit": 2,
                                    },
                                ]
                            }
                        ],
                    },
                    request=request,
                )
            return httpx.Response(403, html="blocked", request=request)

        reference = woocommerce.item_ref(
            "woocommerce",
            {"product_id": 19251, "product_type": "simple", "minimum": 1},
        )
        with self.assertRaisesRegex(ToolError, "cart cleanup failed"):
            woocommerce.quote(
                Http(httpx.MockTransport(handler)), detection(), reference
            )
        self.assertEqual(
            seen[-2:],
            [
                ("DELETE", "/wp-json/wc/store/v1/cart/items/item-secret"),
                ("DELETE", "/wp-json/wc/store/v1/cart/items"),
            ],
        )

    def test_variable_parent_is_not_a_durable_exact_ref(self) -> None:
        with self.assertRaisesRegex(ToolError, "invalid product identity"):
            validate_ref(
                {
                    "platform": "woocommerce",
                    "store": "https://store.example",
                    "product_id": 44,
                    "product_type": "variable",
                    "minimum": 1,
                }
            )

    def test_empty_packages_are_no_quote(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(
                    200,
                    headers={"Cart-Token": "secret"},
                    json={"totals": totals()},
                    request=request,
                )
            if calls == 2:
                return httpx.Response(
                    200, json={"items": [{"id": 7, "key": "key"}]}, request=request
                )
            if calls == 3:
                return httpx.Response(
                    200,
                    json={"totals": totals(), "shipping_rates": []},
                    request=request,
                )
            return httpx.Response(204, request=request)

        reference = woocommerce.item_ref(
            "woocommerce",
            {"product_id": 7, "product_type": "simple", "minimum": 1},
        )
        result = woocommerce.quote(
            Http(httpx.MockTransport(handler)), detection(), reference
        )
        self.assertEqual(result["kind"], "empty")
        self.assertEqual(result["reason"], "empty_rate_list")

    def test_empty_cart_allows_nullable_shipping_totals(self) -> None:
        cart_totals = totals()
        cart_totals["total_shipping"] = None
        cart_totals["total_shipping_tax"] = None
        subtotal, normalized = woocommerce._cart_totals(
            {"totals": cart_totals}, "empty cart"
        )
        self.assertEqual(subtotal, {"amount": "5.95", "currency": "USD"})
        self.assertNotIn("total_shipping", normalized)
        self.assertNotIn("total_shipping_tax", normalized)


if __name__ == "__main__":
    unittest.main()
