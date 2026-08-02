# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.28,<0.29"]
# ///

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures"

from storefront.core import (
    DEFAULT_DESTINATION,
    DetectedStore,
    Session,
    ToolError,
    destination_for,
    item_ref,
    parse_item_ref,
)
from storefront.adapters import squarespace


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def response(
    request: httpx.Request,
    status: int = 200,
    *,
    content: bytes = b"",
    json_value: object | None = None,
    headers: dict[str, str] | list[tuple[str, str]] | None = None,
) -> httpx.Response:
    if json_value is not None:
        content = json.dumps(json_value, separators=(",", ":")).encode()
        headers = (
            [("Content-Type", "application/json"), *(headers or [])]
            if isinstance(headers, list)
            else {
                "Content-Type": "application/json",
                **(headers or {}),
            }
        )
    return httpx.Response(status, content=content, headers=headers, request=request)


def adapter() -> squarespace.Squarespace:
    return squarespace.Squarespace()


def detection(entry_url: str = "https://squarespace.test/") -> DetectedStore:
    return DetectedStore(
        origin="https://squarespace.test",
        entry_url=entry_url,
        platform="squarespace",
        api_origin="https://squarespace.test",
        evidence=("server=Squarespace",),
    )


def reference() -> str:
    return item_ref(
        "squarespace",
        {
            "collection_url": "https://squarespace.test/chairs-and-stools",
            "item_id": "68c79c6651f9b03038220ceb",
            "sku": "SQ3115437",
        },
    )


class SquarespaceTests(unittest.TestCase):
    def test_detects_positive_storefront_markers(self) -> None:
        request = httpx.Request("GET", "https://squarespace.test/")
        homepage = response(
            request,
            content=b'<script src="https://static1.squarespace.com/static/site/app.js"></script>',
            headers={"Server": "Squarespace"},
        )
        detected = squarespace.detect(
            homepage, "https://squarespace.test", "https://squarespace.test/"
        )
        self.assertIsNotNone(detected)
        assert detected is not None
        self.assertEqual(detected.platform, "squarespace")
        self.assertEqual(
            detected.evidence, ("server=Squarespace", "Squarespace static assets")
        )

        generic = response(request, content=b"<html><body>Store</body></html>")
        self.assertIsNone(
            squarespace.detect(
                generic, "https://squarespace.test", "https://squarespace.test/"
            )
        )

    def test_search_discovers_and_hydrates_product_variants(self) -> None:
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if request.url.path == "/search":
                self.assertEqual(request.url.params["q"], "KITSUI")
                return response(
                    request, content=fixture("platform-squarespace-search.html")
                )
            if request.url.path == "/chairs-and-stools/kitsui-occasional-chair":
                self.assertEqual(request.url.params["format"], "json")
                return response(
                    request, content=fixture("platform-squarespace-product.json")
                )
            if request.url.path == "/journal/kitsui-story":
                return response(
                    request,
                    json_value={
                        "website": {"id": "site"},
                        "collection": {"type": 1, "fullUrl": "/journal"},
                        "item": {"id": "post"},
                    },
                )
            raise AssertionError(request.url)

        http = Session(httpx.MockTransport(handler))
        with http.client:
            result = adapter().search(http, detection(), "KITSUI", 20, DEFAULT_DESTINATION)

        self.assertEqual(result["kind"], "search")
        self.assertEqual(result["discovery"], "storefront_search")
        self.assertEqual(len(result["items"]), 2)
        coral = result["items"][0]
        self.assertEqual(coral["title"], "KITSUI LOUNGE CHAIR")
        self.assertEqual(coral["variant"], "Color: Coral")
        self.assertEqual(coral["sku"], "SQ3115437")
        self.assertEqual(coral["price"], {"amount": "1427.00", "currency": "USD"})
        self.assertTrue(coral["available"])
        self.assertEqual(
            parse_item_ref(coral["item_ref"], "squarespace"),
            {
                "collection_url": "https://squarespace.test/chairs-and-stools",
                "item_id": "68c79c6651f9b03038220ceb",
                "sku": "SQ3115437",
            },
        )
        self.assertFalse(any("other.example" in url for url in requested))

    def test_variant_flags_use_documented_missing_defaults(self) -> None:
        payload = json.loads(fixture("platform-squarespace-product.json"))
        variant = payload["item"]["variants"][0]
        variant.pop("onSale")
        variant.pop("unlimited")
        variant.pop("qtyInStock")

        items = squarespace._payload_items(payload, "KITSUI", detection().origin)

        self.assertEqual(items[0]["price"], {"amount": "1427.00", "currency": "USD"})
        self.assertFalse(items[0]["available"])

    def test_variant_rejects_non_boolean_sale_marker(self) -> None:
        for value in (None, "false", 0, 1):
            with self.subTest(value=value):
                payload = json.loads(fixture("platform-squarespace-product.json"))
                payload["item"]["variants"][0]["onSale"] = value

                with self.assertRaisesRegex(ToolError, "onSale"):
                    squarespace._payload_items(payload, "SQ3115437", detection().origin)

    def test_availability_rejects_non_boolean_unlimited_marker(self) -> None:
        for value in (None, "false", 0, 1):
            with self.subTest(value=value), self.assertRaisesRegex(
                ToolError, "unlimited"
            ):
                squarespace._available({"unlimited": value, "qtyInStock": 1})

    def test_availability_rejects_non_integer_stock_quantity(self) -> None:
        for value in (None, "1", 1.0, True):
            with self.subTest(value=value), self.assertRaisesRegex(
                ToolError, "qtyInStock"
            ):
                squarespace._available({"unlimited": False, "qtyInStock": value})

    def test_explicit_collection_entry_skips_storefront_search(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/chairs-and-stools")
            if "format" not in request.url.params:
                return response(request, content=b"storefront")
            self.assertEqual(request.url.params["format"], "json")
            return response(
                request, content=fixture("platform-squarespace-collection.json")
            )

        http = Session(httpx.MockTransport(handler))
        with http.client:
            http.get(
                http.storefront_entry(
                    "https://squarespace.test/chairs-and-stools",
                    ["https://squarespace.test"],
                )
            )
            result = adapter().search(
                http,
                detection("https://squarespace.test/chairs-and-stools"),
                "KITSUI",
                20,
                DEFAULT_DESTINATION,
            )

        self.assertEqual(result["kind"], "search")
        self.assertEqual(result["discovery"], "explicit_entry_url")
        self.assertEqual(
            [item["sku"] for item in result["items"]], ["SQ3115437", "SQ1796788"]
        )

    def test_quote_validates_cart_identity_and_preserves_all_options(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/chairs-and-stools":
                return response(
                    request,
                    content=fixture("platform-squarespace-collection.json"),
                    headers={"Set-Cookie": "crumb=crumb-secret; Path=/; Secure"},
                )
            if request.url.path == "/api/commerce/shopping-cart/entries":
                self.assertEqual(request.headers["x-csrf-token"], "crumb-secret")
                self.assertTrue(request.headers["add-to-cart-id"])
                self.assertEqual(
                    json.loads(request.content),
                    {
                        "itemId": "68c79c6651f9b03038220ceb",
                        "sku": "SQ3115437",
                        "quantity": 1,
                        "additionalFields": None,
                    },
                )
                return response(
                    request, content=fixture("platform-squarespace-add.json")
                )
            if (
                request.url.path
                == "/api/3/commerce/cart/cart-token-fixture/shipping/location"
            ):
                self.assertEqual(
                    json.loads(request.content),
                    destination_for("squarespace", DEFAULT_DESTINATION),
                )
                return response(
                    request, content=fixture("platform-squarespace-shipping.json")
                )
            raise AssertionError(request.url)

        http = Session(httpx.MockTransport(handler))
        with http.client:
            result = adapter().quote(http, detection(), [{"ref": reference(), "quantity": 1}], DEFAULT_DESTINATION)

        self.assertEqual(result["kind"], "quote")
        self.assertEqual(result["subtotal"], {"amount": "1427.00", "currency": "USD"})
        self.assertEqual(
            result["shipping_options_status"], "APPLICABLE_SHIPPING_OPTIONS"
        )
        self.assertEqual(
            [option["disposition"] for option in result["shipping_options"]],
            ["delivery", "delivery", "pickup"],
        )
        self.assertEqual(
            [rate["amount"] for rate in result["rates"]],
            [
                {"amount": "161.13", "currency": "USD"},
                {"amount": "562.13", "currency": "USD"},
            ],
        )
        durable = json.dumps({"result": result, "evidence": http.evidence})
        for secret in ("crumb-secret", "cart-token-fixture"):
            self.assertNotIn(secret, durable)

    def test_fulfillment_pickup_marker_requires_boolean(self) -> None:
        option = {
            "key": "ground",
            "name": "Ground",
            "price": {"decimalValue": "12.00", "currencyCode": "USD"},
            "isPickup": "false",
        }

        with self.assertRaisesRegex(ToolError, "isPickup"):
            squarespace._shipping_option(option)

    def test_quote_rejects_cart_identity_mismatch(self) -> None:
        added = json.loads(fixture("platform-squarespace-add.json"))
        added["newlyAdded"]["chosenVariant"]["sku"] = "WRONG"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/chairs-and-stools":
                return response(
                    request,
                    content=fixture("platform-squarespace-collection.json"),
                    headers={"Set-Cookie": "crumb=crumb-secret; Path=/; Secure"},
                )
            if request.url.path == "/api/commerce/shopping-cart/entries":
                return response(request, json_value=added)
            raise AssertionError("shipping must not run")

        http = Session(httpx.MockTransport(handler))
        with http.client, self.assertRaisesRegex(ToolError, "selected product"):
            adapter().quote(http, detection(), [{"ref": reference(), "quantity": 1}], DEFAULT_DESTINATION)

    def test_quote_distinguishes_non_applicable_shipping_statuses(self) -> None:
        for status, reason in (
            ("SHIPPING_NOT_REQUIRED", "shipping_not_required"),
            ("POSTAL_CODE_NOT_APPLICABLE", "postal_code_not_applicable"),
        ):
            with self.subTest(status=status):
                shipping = {
                    "shippingOptionsStatus": status,
                    "subtotal": {"currencyCode": "USD", "decimalValue": "1427.00"},
                    "fulfillmentOptions": [],
                }

                def handler(
                    request: httpx.Request,
                    shipping: dict[str, object] = shipping,
                ) -> httpx.Response:
                    if request.url.path == "/chairs-and-stools":
                        return response(
                            request,
                            content=fixture("platform-squarespace-collection.json"),
                            headers={
                                "Set-Cookie": "crumb=crumb-secret; Path=/; Secure"
                            },
                        )
                    if request.url.path == "/api/commerce/shopping-cart/entries":
                        return response(
                            request, content=fixture("platform-squarespace-add.json")
                        )
                    if (
                        request.url.path
                        == "/api/3/commerce/cart/cart-token-fixture/shipping/location"
                    ):
                        return response(request, json_value=shipping)
                    raise AssertionError(request.url)

                http = Session(httpx.MockTransport(handler))
                with http.client:
                    result = adapter().quote(http, detection(), [{"ref": reference(), "quantity": 1}], DEFAULT_DESTINATION)
                self.assertEqual(result["kind"], "empty")
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["shipping_options_status"], status)


if __name__ == "__main__":
    unittest.main()
