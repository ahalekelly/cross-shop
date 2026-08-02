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
    Http,
    ToolError,
    destination_for,
    item_ref,
    parse_item_ref,
)
from storefront.adapters import bigcommerce


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


def adapter() -> bigcommerce.BigCommerce:
    return bigcommerce.BigCommerce()


def detection() -> DetectedStore:
    return DetectedStore(
        origin="https://bigcommerce.test",
        entry_url="https://bigcommerce.test/",
        platform="bigcommerce",
        api_origin="https://bigcommerce.test",
        evidence=("bigcommerce_stencil",),
    )


class BigCommerceTests(unittest.TestCase):
    def test_detects_positive_storefront_markers(self) -> None:
        request = httpx.Request("GET", "https://bigcommerce.test/")
        homepage = response(
            request,
            content=b'<script src="https://cdn11.bigcommerce.com/s-store123/stencil/app.js"></script>',
            headers={"Server": "BigCommerce"},
        )
        detected = bigcommerce.detect(
            homepage, "https://bigcommerce.test", "https://bigcommerce.test/"
        )
        self.assertIsNotNone(detected)
        assert detected is not None
        self.assertEqual(detected.platform, "bigcommerce")
        self.assertEqual(
            detected.evidence,
            (
                "server=BigCommerce",
                "BigCommerce CDN store hash",
                "BigCommerce Stencil assets",
            ),
        )

        generic = response(request, content=b"<html><body>Store</body></html>")
        self.assertIsNone(
            bigcommerce.detect(
                generic, "https://bigcommerce.test", "https://bigcommerce.test/"
            )
        )

    def test_search_ignores_add_to_cart_anchors(self) -> None:
        product_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search.php":
                return response(
                    request,
                    content=b"""
                    <a href="/cart.php?action=add&amp;product_id=123"
                       class="button card-figcaption-button"
                       data-button-type="add-cart" data-product-id="123">
                      Add to Cart
                    </a>
                    <a href="/precision-bearing/" data-card-type="product"
                       data-product-id="123"
                       data-sku="BRG-1" title="Precision Bearing">
                      Precision Bearing
                    </a>
                    """,
                    headers={"Content-Type": "text/html"},
                )
            product_calls.append(str(request.url))
            if request.url.path != "/precision-bearing/":
                raise AssertionError(request.url)
            return response(
                request,
                content=fixture("platform-bigcommerce-product.html"),
                headers={"Content-Type": "text/html"},
            )

        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = adapter().search(http, detection(), "bearing", 20, DEFAULT_DESTINATION)

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(product_calls, ["https://bigcommerce.test/precision-bearing/"])

    def test_search_ignores_login_cta_with_generic_card_styling(self) -> None:
        product_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search.php":
                return response(
                    request,
                    content=b"""
                    <a href="/login.php" class="button card-figcaption-button">
                      Sign in to buy
                    </a>
                    <a href="/precision-bearing/" data-product-id="123"
                       data-sku="BRG-1" title="Precision Bearing">
                      Precision Bearing
                    </a>
                    """,
                    headers={"Content-Type": "text/html"},
                )
            product_calls.append(str(request.url))
            if request.url.path != "/precision-bearing/":
                raise AssertionError(request.url)
            return response(
                request,
                content=fixture("platform-bigcommerce-product.html"),
                headers={"Content-Type": "text/html"},
            )

        result = adapter().search(
            Http(httpx.MockTransport(handler)), detection(), "bearing", 20, DEFAULT_DESTINATION
        )

        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(product_calls, ["https://bigcommerce.test/precision-bearing/"])

    def test_search_hydrates_three_exact_ranked_query_free_products(self) -> None:
        product_page = fixture("platform-bigcommerce-product.html")
        product_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search.php":
                self.assertEqual(request.url.params["search_query"], "BRG-1")
                return response(
                    request,
                    content=fixture("platform-bigcommerce-search.html"),
                    headers={"Content-Type": "text/html"},
                )
            product_calls.append(str(request.url))
            replacements = {
                "/precision-bearing/": (b"BRG-1", b"123", b"Precision"),
                "/other-bearing/": (b"BRG-10", b"122", b"Other"),
                "/third-bearing/": (b"X-BRG-1", b"121", b"Third"),
            }
            if request.url.path not in replacements:
                raise AssertionError(request.url)
            sku, product_id, title = replacements[request.url.path]
            body = (
                product_page.replace(b"BRG-1", sku)
                .replace(b"123", product_id)
                .replace(b"Precision", title)
            )
            return response(
                request, content=body, headers={"Content-Type": "text/html"}
            )

        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = adapter().search(http, detection(), "BRG-1", 20, DEFAULT_DESTINATION)

        self.assertEqual(result["kind"], "search")
        self.assertEqual(len(result["items"]), 3)
        exact = result["items"][0]
        self.assertEqual(exact["sku"], "BRG-1")
        self.assertEqual(exact["price"], {"amount": "12.5", "currency": "USD"})
        self.assertEqual(exact["compare_at_price"], {"amount": "15", "currency": "USD"})
        self.assertEqual(exact["weight"], {"value": 0.1, "unit": "POUNDS"})
        self.assertTrue(exact["available"])
        self.assertFalse(exact["requires_configuration"])
        selected = parse_item_ref(exact["item_ref"], "bigcommerce")
        self.assertEqual(
            selected,
            {
                "product_id": 123,
                "product_url": "https://bigcommerce.test/precision-bearing/",
            },
        )
        self.assertTrue(all("?" not in url and "#" not in url for url in product_calls))
        self.assertNotIn("fourth-bearing", " ".join(product_calls))

    def test_search_preserves_quote_only_product_without_inventing_a_price(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/search.php":
                return response(
                    request,
                    content=b"""
                    <a href="/quote-bearing/" data-card-type="product"
                       data-sku="QUOTE-1" title="Quote Bearing">Quote Bearing</a>
                    <a href="/precision-bearing/" data-card-type="product"
                       data-sku="BRG-1" title="Precision Bearing">Precision Bearing</a>
                    """,
                    headers={"Content-Type": "text/html"},
                )
            if request.url.path == "/quote-bearing/":
                return response(
                    request,
                    content=(
                        b"<script>var BCData = "
                        b'{"product_attributes":{"sku":"QUOTE-1","weight":null,'
                        b'"instock":true,"purchasable":false}};</script>'
                        b"<h1>Quote Bearing</h1>"
                        b'<form><input name="product_id" value="400"></form>'
                    ),
                    headers={"Content-Type": "text/html"},
                )
            if request.url.path == "/precision-bearing/":
                return response(
                    request,
                    content=fixture("platform-bigcommerce-product.html"),
                    headers={"Content-Type": "text/html"},
                )
            raise AssertionError(request.url)

        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = adapter().search(http, detection(), "bearing", 20, DEFAULT_DESTINATION)

        self.assertEqual(len(result["items"]), 2)
        quote_only = result["items"][0]
        self.assertEqual(quote_only["sku"], "QUOTE-1")
        self.assertTrue(quote_only["available"])
        self.assertFalse(quote_only["purchasable"])
        self.assertIsNone(quote_only["price"])
        self.assertEqual(result["items"][1]["sku"], "BRG-1")

    def test_shipping_option_defaults_missing_pickup_marker_to_false(self) -> None:
        option = {
            "id": "ground",
            "type": "flat",
            "description": "Ground",
            "cost": 10,
        }

        result = bigcommerce._shipping_option(option, "USD")

        self.assertEqual(result["disposition"], "delivery")

    def test_shipping_option_rejects_non_boolean_pickup_marker(self) -> None:
        option = {
            "id": "ground",
            "type": "flat",
            "description": "Ground",
            "cost": 10,
        }

        for value in (None, "false", 0, 1):
            with self.subTest(value=value), self.assertRaisesRegex(
                ToolError, "isPickup"
            ):
                bigcommerce._shipping_option({**option, "isPickup": value}, "USD")

    def test_quote_uses_clean_rest_cart_and_preserves_option_semantics(self) -> None:
        checkout = json.loads(fixture("platform-bigcommerce-shipping.json"))

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/precision-bearing/":
                return response(
                    request,
                    content=fixture("platform-bigcommerce-product.html"),
                    headers={"Content-Type": "text/html"},
                )
            if request.url.path == "/api/storefront/carts":
                self.assertNotIn("cookie", request.headers)
                self.assertEqual(
                    json.loads(request.content),
                    {
                        "lineItems": [
                            {"quantity": 1, "productId": 123, "optionSelections": []}
                        ]
                    },
                )
                return response(
                    request,
                    json_value={
                        "id": "cart-secret",
                        "baseAmount": 12.5,
                        "currency": {"code": "USD"},
                        "lineItems": {
                            "physicalItems": [
                                {
                                    "id": "item-secret",
                                    "productId": 123,
                                    "sku": "BRG-1",
                                    "isShippingRequired": True,
                                }
                            ]
                        },
                    },
                    headers=[
                        (
                            "Set-Cookie",
                            "SHOP_SESSION_TOKEN=session-secret; Path=/; Secure",
                        ),
                        ("Set-Cookie", "SF-CSRF-TOKEN=csrf-secret; Path=/; Secure"),
                    ],
                )
            if request.url.path == "/api/storefront/checkouts/cart-secret/consignments":
                self.assertEqual(
                    request.headers["cookie"], "SHOP_SESSION_TOKEN=session-secret"
                )
                self.assertEqual(request.headers["x-sf-csrf-token"], "csrf-secret")
                body = json.loads(request.content)
                self.assertEqual(
                    body[0]["address"],
                    destination_for("bigcommerce", DEFAULT_DESTINATION),
                )
                self.assertEqual(
                    body[0]["lineItems"], [{"itemId": "item-secret", "quantity": 1}]
                )
                return response(request, json_value=checkout)
            raise AssertionError(request.url)

        http = Http(httpx.MockTransport(handler))
        reference = item_ref(
            "bigcommerce",
            {
                "product_id": 123,
                "product_url": "https://bigcommerce.test/precision-bearing/",
            },
        )
        with http.client:
            result = adapter().quote(http, detection(), [{"ref": reference, "quantity": 1}], DEFAULT_DESTINATION)

        self.assertEqual(result["kind"], "quote")
        self.assertEqual(result["subtotal"], {"amount": "12.5", "currency": "USD"})
        self.assertEqual(
            [value["disposition"] for value in result["shipping_options"]],
            [
                "delivery",
                "pickup",
                "paid_later",
                "unavailable",
            ],
        )
        self.assertEqual(
            [value["option_id"] for value in result["rates"]], ["delivery"]
        )
        durable = json.dumps({"result": result, "evidence": http.evidence})
        for secret in ("cart-secret", "item-secret", "session-secret", "csrf-secret"):
            self.assertNotIn(secret, durable)

    def test_quote_rejects_boolean_cart_product_id(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/precision-bearing/":
                return response(
                    request,
                    content=fixture("platform-bigcommerce-product.html").replace(
                        b'value="123"', b'value="1"'
                    ),
                    headers={"Content-Type": "text/html"},
                )
            if request.url.path == "/api/storefront/carts":
                return response(
                    request,
                    json_value={
                        "id": "cart-secret",
                        "currency": {"code": "USD"},
                        "lineItems": {
                            "physicalItems": [
                                {
                                    "id": "wrong-item",
                                    "productId": True,
                                    "isShippingRequired": True,
                                }
                            ]
                        },
                    },
                )
            raise AssertionError("consignment must not run")

        reference = item_ref(
            "bigcommerce",
            {
                "product_id": 1,
                "product_url": "https://bigcommerce.test/precision-bearing/",
            },
        )
        http = Http(httpx.MockTransport(handler))
        with http.client, self.assertRaisesRegex(ToolError, "exactly the selected"):
            adapter().quote(http, detection(), [{"ref": reference, "quantity": 1}], DEFAULT_DESTINATION)

    def test_configuration_and_stale_identity_fail_before_cart(self) -> None:
        def configurable(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/configured/":
                return response(
                    request,
                    content=fixture("platform-bigcommerce-product-configurable.html"),
                    headers={"Content-Type": "text/html"},
                )
            raise AssertionError("cart must not run")

        http = Http(httpx.MockTransport(configurable))
        reference = item_ref(
            "bigcommerce",
            {"product_id": 124, "product_url": "https://bigcommerce.test/configured/"},
        )
        with http.client:
            result = adapter().quote(http, detection(), [{"ref": reference, "quantity": 1}], DEFAULT_DESTINATION)
        self.assertEqual(result["status"], "api_error")
        self.assertEqual(result["fields"], ["attribute[7]"])

        http = Http(httpx.MockTransport(configurable))
        stale = item_ref(
            "bigcommerce",
            {"product_id": 125, "product_url": "https://bigcommerce.test/configured/"},
        )
        with http.client, self.assertRaisesRegex(ToolError, "does not match"):
            adapter().quote(http, detection(), [{"ref": stale, "quantity": 1}], DEFAULT_DESTINATION)

    def test_item_ref_rejects_tracking_query(self) -> None:
        http = Http(
            httpx.MockTransport(lambda request: self.fail("request must not run"))
        )
        reference = item_ref(
            "bigcommerce",
            {
                "product_id": 123,
                "product_url": "https://bigcommerce.test/precision-bearing/?searchid=1",
            },
        )
        with http.client, self.assertRaisesRegex(ToolError, "canonical"):
            adapter().quote(http, detection(), [{"ref": reference, "quantity": 1}], DEFAULT_DESTINATION)


if __name__ == "__main__":
    unittest.main()
