# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.28,<0.29"]
# ///

from __future__ import annotations

import copy
import importlib
import json
import sys
import unittest
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(ROOT))

core = importlib.import_module("storefront.core")
magento = importlib.import_module("storefront.adapters.magento")
MagentoDetectedStore = core.MagentoDetectedStore
Http = core.Http
ToolError = core.ToolError
parse_item_ref = core.parse_item_ref
DEFAULT_DESTINATION = core.DEFAULT_DESTINATION


def adapter():
    return magento.Magento()


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> Any:
    return json.loads(fixture(name))


def response(
    request: httpx.Request,
    status: int = 200,
    *,
    json_value: Any = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    if content is not None:
        return httpx.Response(status, content=content, headers=headers, request=request)
    return httpx.Response(status, json=json_value, headers=headers, request=request)


def detection(source: str = "graphql") -> MagentoDetectedStore:
    return MagentoDetectedStore(
        origin="https://magento.test",
        entry_url="https://magento.test/",
        api_origin="https://magento.test",
        evidence=("magento_graphql_product_search",),
        search_source=source,
    )


class MagentoDetectionTests(unittest.TestCase):
    def homepage(self, content: bytes = b"<html></html>") -> httpx.Response:
        request = httpx.Request("GET", "https://magento.test/")
        return response(request, content=content)

    def test_product_search_capability_is_positive_read_only_evidence(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            return response(
                request,
                json_value={
                    "data": {
                        "storeConfig": {"base_url": "https://magento.test/"},
                        "products": {"total_count": 0},
                    }
                },
            )

        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = magento.detect(
                http,
                "https://magento.test",
                "https://magento.test/",
                self.homepage(),
            )

        self.assertEqual(result.platform, "magento")
        self.assertEqual(result.search_source, "graphql")
        self.assertEqual(result.evidence, ("magento_graphql_product_search",))
        self.assertEqual(calls, [("POST", "/graphql")])

    def test_unavailable_graphql_with_homepage_proof_selects_html(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return response(request, 400)

        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = magento.detect(
                http,
                "https://magento.test",
                "https://magento.test/",
                self.homepage(b'<script type="text/x-magento-init">{}</script>'),
            )

        self.assertEqual(result.search_source, "html")
        self.assertEqual(result.evidence, ("magento_x_magento_init",))

    def test_graphql_store_config_without_products_is_contradictory(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return response(
                request,
                json_value={
                    "data": {"storeConfig": {"base_url": "https://magento.test/"}}
                },
            )

        http = Http(httpx.MockTransport(handler))
        with http.client, self.assertRaisesRegex(ToolError, "contradictory"):
            magento.detect(
                http,
                "https://magento.test",
                "https://magento.test/",
                self.homepage(),
            )

    def test_generic_denials_without_magento_marker_do_not_detect(self) -> None:
        http = Http(
            httpx.MockTransport(
                lambda request: response(request, 403, content=b"Forbidden")
            )
        )
        with http.client:
            result = magento.detect(
                http,
                "https://magento.test",
                "https://magento.test/",
                self.homepage(b'<input name="form_key" value="not-platform-specific">'),
            )

        self.assertIsNone(result)

    def test_homepage_marker_survives_denied_probes(self) -> None:
        http = Http(
            httpx.MockTransport(
                lambda request: response(request, 403, content=b"Forbidden")
            )
        )
        with http.client:
            result = magento.detect(
                http,
                "https://magento.test",
                "https://magento.test/",
                self.homepage(b'<script type="text/x-magento-init">{}</script>'),
            )

        self.assertEqual(result.evidence, ("magento_x_magento_init",))


class MagentoSearchTests(unittest.TestCase):
    def test_exact_top_level_sku_takes_precedence_over_matching_child(self) -> None:
        price_range = {
            "minimum_price": {
                "final_price": {"value": 10, "currency": "USD"},
                "regular_price": {"value": 10, "currency": "USD"},
            }
        }
        envelope = {
            "data": {
                "storeConfig": {"product_url_suffix": ""},
                "products": {
                    "items": [
                        {
                            "__typename": "SimpleProduct",
                            "name": "Exact product",
                            "sku": "SAME",
                            "stock_status": "IN_STOCK",
                            "url_key": "exact-product",
                            "price_range": price_range,
                        },
                        {
                            "__typename": "ConfigurableProduct",
                            "name": "Unrelated parent",
                            "sku": "PARENT",
                            "url_key": "unrelated-parent",
                            "variants": [
                                {
                                    "attributes": [],
                                    "product": {
                                        "__typename": "SimpleProduct",
                                        "name": "Matching child",
                                        "sku": "SAME",
                                        "stock_status": "IN_STOCK",
                                        "price_range": price_range,
                                    },
                                }
                            ],
                        },
                    ]
                },
            }
        }

        items, omitted = magento._detail_items(envelope, "SAME", "https://store.test/")

        self.assertEqual(items[0]["title"], "Exact product")
        self.assertEqual(omitted, 0)

    def test_duplicate_exact_configurable_children_fail_loudly(self) -> None:
        child = {
            "attributes": [],
            "product": {
                "__typename": "SimpleProduct",
                "name": "Matching child",
                "sku": "SAME",
            },
        }
        envelope = {
            "data": {
                "storeConfig": {"product_url_suffix": ""},
                "products": {
                    "items": [
                        {
                            "__typename": "ConfigurableProduct",
                            "name": "Parent",
                            "sku": "PARENT",
                            "url_key": "parent",
                            "variants": [child, copy.deepcopy(child)],
                        }
                    ]
                },
            }
        }

        with self.assertRaisesRegex(ToolError, "child SKU resolved more than once"):
            magento._detail_items(envelope, "SAME", "https://store.test/")

    def test_sku_detail_accepts_one_exact_configurable_child(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if "ProductSearch" in body["query"]:
                return response(
                    request,
                    json_value={
                        "data": {
                            "products": {
                                "items": [
                                    {
                                        "__typename": "SimpleProduct",
                                        "name": "Extension Open Source",
                                        "sku": "CHILD",
                                        "stock_status": "IN_STOCK",
                                        "url_key": "extension",
                                    }
                                ]
                            }
                        }
                    },
                )
            return response(
                request,
                json_value={
                    "data": {
                        "storeConfig": {"product_url_suffix": ""},
                        "products": {
                            "items": [
                                {
                                    "__typename": "ConfigurableProduct",
                                    "name": "Extension",
                                    "sku": "PARENT",
                                    "url_key": "extension",
                                    "variants": [
                                        {
                                            "attributes": [
                                                {
                                                    "code": "license",
                                                    "label": "Open Source",
                                                    "value_index": 1,
                                                }
                                            ],
                                            "product": {
                                                "__typename": "SimpleProduct",
                                                "name": "Extension Open Source",
                                                "sku": "CHILD",
                                                "stock_status": "IN_STOCK",
                                                "price_range": {
                                                    "minimum_price": {
                                                        "final_price": {
                                                            "value": 199,
                                                            "currency": "USD",
                                                        },
                                                        "regular_price": {
                                                            "value": 199,
                                                            "currency": "USD",
                                                        },
                                                    }
                                                },
                                            },
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                },
            )

        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = adapter().search(http, detection(), "extension", 20, DEFAULT_DESTINATION)

        self.assertEqual([item["sku"] for item in result["items"]], ["CHILD"])

    def test_missing_exact_sku_is_recorded_per_candidate(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if "ProductSearch" in body["query"]:
                return response(
                    request,
                    json_value={
                        "data": {
                            "products": {
                                "items": [
                                    {
                                        "__typename": "SimpleProduct",
                                        "name": "Missing",
                                        "sku": "MISSING",
                                        "stock_status": "IN_STOCK",
                                        "url_key": "missing",
                                    },
                                    {
                                        "__typename": "SimpleProduct",
                                        "name": "Good",
                                        "sku": "GOOD",
                                        "stock_status": "IN_STOCK",
                                        "url_key": "good",
                                    },
                                ]
                            }
                        }
                    },
                )
            sku = body["variables"]["sku"]
            items = []
            if sku == "GOOD":
                items.append(
                    {
                        "__typename": "SimpleProduct",
                        "name": "Good",
                        "sku": "GOOD",
                        "stock_status": "IN_STOCK",
                        "url_key": "good",
                        "price_range": {
                            "minimum_price": {
                                "final_price": {"value": 10, "currency": "USD"},
                                "regular_price": {"value": 10, "currency": "USD"},
                            }
                        },
                    }
                )
            return response(
                request,
                json_value={
                    "data": {
                        "storeConfig": {"product_url_suffix": ""},
                        "products": {"items": items},
                    }
                },
            )

        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = adapter().search(http, detection(), "bearing", 20, DEFAULT_DESTINATION)

        self.assertEqual([item["sku"] for item in result["items"]], ["GOOD"])
        self.assertEqual(result["api_errors"][0]["stage"], "detail")
        self.assertEqual(result["api_errors"][0]["sku"], "MISSING")

    def test_html_strategy_uses_canonical_search_route_without_trailing_slash(
        self,
    ) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/catalogsearch/result":
                self.assertEqual(request.url.params["q"], "bearing")
                return response(request, content=b"<html></html>")
            raise AssertionError(request.url)

        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = adapter().search(http, detection("html"), "bearing", 20, DEFAULT_DESTINATION)

        self.assertEqual(result["source"], "html")
        self.assertEqual(paths, ["/catalogsearch/result"])

    def test_html_strategy_accepts_one_same_origin_canonical_redirect(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/catalogsearch/result":
                return response(
                    request,
                    302,
                    headers={"Location": "/search/bearing"},
                )
            if request.url.path == "/search/bearing":
                return response(request, content=b"<html></html>")
            raise AssertionError(request.url)

        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = adapter().search(http, detection("html"), "bearing", 20, DEFAULT_DESTINATION)

        self.assertEqual(result["source"], "html")
        self.assertEqual(paths, ["/catalogsearch/result", "/search/bearing"])

    def test_html_strategy_rejects_cross_origin_redirect(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return response(
                request,
                302,
                headers={"Location": "https://other.test/search/bearing"},
            )

        http = Http(httpx.MockTransport(handler))
        with http.client, self.assertRaisesRegex(ToolError, "same storefront"):
            adapter().search(http, detection("html"), "bearing", 20, DEFAULT_DESTINATION)

    def test_graphql_detail_accepts_null_suffix_and_uses_detected_entry_url(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if "ProductSearch" in body["query"]:
                return response(
                    request,
                    json_value={
                        "data": {
                            "products": {
                                "items": [
                                    {
                                        "__typename": "SimpleProduct",
                                        "name": "Bearing",
                                        "sku": "BRG-1",
                                        "stock_status": "IN_STOCK",
                                        "url_key": "bearing",
                                    }
                                ]
                            }
                        }
                    },
                )
            self.assertIn("products(search: $sku, pageSize: 20)", body["query"])
            self.assertEqual(body["variables"], {"sku": "BRG-1"})
            self.assertNotIn("base_url", body["query"])
            self.assertNotIn("base_currency_code", body["query"])
            return response(
                request,
                json_value={
                    "data": {
                        "storeConfig": {
                            "base_url": "http://merchant-controlled.example/",
                            "product_url_suffix": None,
                        },
                        "products": {
                            "items": [
                                {
                                    "__typename": "SimpleProduct",
                                    "name": "Bearing",
                                    "sku": "BRG-1",
                                    "stock_status": "IN_STOCK",
                                    "url_key": "bearing",
                                    "price_range": {
                                        "minimum_price": {
                                            "final_price": {
                                                "value": 2.5,
                                                "currency": "USD",
                                            },
                                            "regular_price": {
                                                "value": 2.5,
                                                "currency": "USD",
                                            },
                                        }
                                    },
                                }
                            ]
                        },
                    }
                },
            )

        custom_detection = MagentoDetectedStore(
            origin="https://magento.test",
            entry_url="https://magento.test/us/",
            api_origin="https://magento.test",
            evidence=("magento_graphql_product_search",),
            search_source="graphql",
        )
        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = adapter().search(http, custom_detection, "bearing", 20, DEFAULT_DESTINATION)

        self.assertEqual(result["items"][0]["url"], "https://magento.test/us/bearing")

    def test_configurable_page_does_not_emit_plain_product_json_ld(self) -> None:
        page = magento.ProductPage()
        page.feed(
            """
            <script type="application/ld+json">
              {
                "@type": "Product",
                "name": "Configurable Bearing",
                "sku": "BRG-PARENT",
                "offers": {"price": 3.95, "priceCurrency": "USD"}
              }
            </script>
            <form id="product_addtocart_form" data-product-sku="BRG-PARENT">
              <select name="super_attribute[93]" required></select>
            </form>
            """
        )

        items, configurable = magento._page_items(
            page, "https://magento.test/configurable-bearing.html"
        )

        self.assertTrue(configurable)
        self.assertEqual(items, [])

    def test_graphql_detail_requires_storefront_url_fields(self) -> None:
        cases = [
            ("product_url_suffix", "storeConfig.product_url_suffix"),
            ("url_key", "product url_key"),
        ]
        for field, message in cases:
            with self.subTest(field=field):
                detail = fixture_json("platform-magento-detail-partial.json")
                if field == "url_key":
                    detail["data"]["products"]["items"][0].pop(field)
                else:
                    detail["data"]["storeConfig"].pop(field)

                with self.assertRaisesRegex(ToolError, message):
                    magento._detail_items(detail, "BRG-PARENT", "https://magento.test/")

    def test_url_key_detail_selects_the_exact_parent_sku(self) -> None:
        detail = fixture_json("platform-magento-detail-partial.json")
        other = dict(detail["data"]["products"]["items"][0])
        other["sku"] = "BRG-OTHER"
        detail["data"]["products"]["items"].insert(0, other)

        items, omitted = magento._detail_items(
            detail, "BRG-PARENT", "https://magento.test/"
        )

        self.assertEqual(omitted, 0)
        self.assertEqual([item["sku"] for item in items], ["BRG-STEEL"])

    def test_graphql_detail_rejects_non_nullable_suffix_types(self) -> None:
        for suffix in (False, 7, {}, []):
            with self.subTest(suffix=suffix):
                detail = fixture_json("platform-magento-detail-partial.json")
                detail["data"]["storeConfig"]["product_url_suffix"] = suffix

                with self.assertRaisesRegex(ToolError, "must be a string or null"):
                    magento._detail_items(detail, "BRG-PARENT", "https://magento.test/")

    def test_narrow_search_then_exact_detail_preserves_partial_errors(self) -> None:
        calls: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            calls.append(body)
            query = body["query"]
            if "ProductSearch" in query:
                self.assertNotIn("variants", query)
                self.assertEqual(body["variables"], {"search": "bearing"})
                return response(
                    request, content=fixture("platform-magento-search-partial.json")
                )
            self.assertIn("products(search: $sku, pageSize: 20)", query)
            self.assertIn("pageSize: 20", query)
            sku = body["variables"]["sku"]
            if sku == "BRG-PARENT":
                return response(
                    request, content=fixture("platform-magento-detail-partial.json")
                )
            if sku == "BRG-1":
                return response(
                    request,
                    json_value={
                        "data": {
                            "storeConfig": {
                                "base_url": "https://magento.test/",
                                "product_url_suffix": ".html",
                                "base_currency_code": "USD",
                            },
                            "products": {
                                "items": [
                                    {
                                        "__typename": "SimpleProduct",
                                        "name": "Bearing",
                                        "sku": "BRG-1",
                                        "stock_status": "IN_STOCK",
                                        "url_key": "bearing",
                                        "price_range": {
                                            "minimum_price": {
                                                "final_price": {
                                                    "value": 2.5,
                                                    "currency": "USD",
                                                },
                                                "regular_price": {
                                                    "value": 3,
                                                    "currency": "USD",
                                                },
                                            }
                                        },
                                    }
                                ]
                            },
                        }
                    },
                )
            raise AssertionError(sku)

        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = adapter().search(http, detection(), "bearing", 20, DEFAULT_DESTINATION)

        self.assertEqual(
            [item["sku"] for item in result["items"]], ["BRG-1", "BRG-STEEL"]
        )
        self.assertEqual(
            parse_item_ref(result["items"][1]["item_ref"], "magento"),
            {"sku": "BRG-STEEL"},
        )
        self.assertEqual(
            result["items"][0]["compare_at_price"], {"amount": "3", "currency": "USD"}
        )
        self.assertEqual(
            result["items"][1]["options"],
            [{"code": "material", "label": "Steel", "value": 7}],
        )
        self.assertEqual(len(result["api_errors"]), 2)
        self.assertEqual(len(calls), 3)

    def test_html_strategy_is_bounded_to_same_origin(self) -> None:
        fetched: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            fetched.append(str(request.url))
            if request.url.path == "/catalogsearch/result":
                self.assertEqual(request.url.params["q"], "bearing")
                return response(
                    request, content=fixture("platform-magento-search.html")
                )
            if request.url.path == "/configurable-bearing.html":
                return response(
                    request,
                    content=fixture("platform-magento-product-configurable.html"),
                )
            if request.url.path == "/simple-bearing.html":
                return response(
                    request, content=fixture("platform-magento-product-simple.html")
                )
            if request.url.path == "/removed-bearing.html":
                self.assertFalse(request.url.query)
                return response(request, 404)
            raise AssertionError(request.url)

        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = adapter().search(http, detection("html"), "bearing", 20, DEFAULT_DESTINATION)

        self.assertEqual(result["kind"], "search")
        self.assertEqual(result["source"], "html")
        self.assertEqual(
            [(item["sku"], item["available"]) for item in result["items"]],
            [("BRG-STEEL", True), ("BRG-BRONZE", False), ("BRG-SIMPLE", True)],
        )
        self.assertEqual(
            result["items"][2]["price"], {"amount": "6.25", "currency": "USD"}
        )
        self.assertFalse(
            any("other.test" in url or "mailto:" in url for url in fetched)
        )

    def test_html_denial_is_gated(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/catalogsearch/result":
                return response(request, 403, content=b"Forbidden")
            raise AssertionError(request.url)

        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = adapter().search(http, detection("html"), "bearing", 20, DEFAULT_DESTINATION)

        self.assertEqual(result["status"], "api_error")
        self.assertEqual(result["stage"], "search")

    def test_canonical_html_search_failure_is_not_retried(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/catalogsearch/result":
                return response(request, 500)
            raise AssertionError(request.url)

        http = Http(httpx.MockTransport(handler))
        with (
            http.client,
            self.assertRaisesRegex(ToolError, "Magento HTML search returned HTTP 500"),
        ):
            adapter().search(http, detection("html"), "bearing", 20, DEFAULT_DESTINATION)

        self.assertEqual(paths, ["/catalogsearch/result"])

    def test_html_challenge_is_bot_wall(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return response(
                request,
                403,
                content=b'<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>',
            )

        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = adapter().search(http, detection("html"), "bearing", 20, DEFAULT_DESTINATION)

        self.assertEqual(result["status"], "api_error")
        self.assertEqual(result["state"], "bot_wall")


class MagentoQuoteTests(unittest.TestCase):
    def handler(self, rates: list[dict[str, Any]] | None = None):
        token = "guest-token-secret-1234567890"
        calls: list[tuple[str, str]] = []
        quote_rates = (
            fixture_json("platform-magento-rates.json") if rates is None else rates
        )

        def handle(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.url.path == "/rest/V1/guest-carts":
                return response(request, json_value=token)
            if request.url.path.endswith("/items"):
                body = json.loads(request.content)
                self.assertEqual(
                    body,
                    {"cartItem": {"sku": "BRG-STEEL", "qty": 1, "quote_id": token}},
                )
                return response(
                    request,
                    json_value={
                        "sku": "BRG-STEEL",
                        "name": "Configurable Bearing - Steel",
                        "qty": 1,
                        "price": 3.95,
                    },
                )
            if request.url.path.endswith("/totals"):
                return response(
                    request, content=fixture("platform-magento-totals.json")
                )
            if request.url.path.endswith("/estimate-shipping-methods"):
                body = json.loads(request.content)
                self.assertEqual(body["address"]["region"], "CA")
                return response(request, json_value=quote_rates)
            raise AssertionError(request.url)

        return handle, calls, token

    def test_exact_sku_cart_totals_currency_and_rate_dispositions(self) -> None:
        handler, calls, token = self.handler()
        http = Http(httpx.MockTransport(handler))
        reference = magento.item_ref("magento", {"sku": "BRG-STEEL"})
        with http.client:
            result = adapter().quote(
                http, detection(), [{"ref": reference, "quantity": 1}], DEFAULT_DESTINATION
            )

        self.assertEqual(result["kind"], "quote")
        self.assertEqual(result["subtotal"], {"amount": "3.95", "currency": "USD"})
        self.assertEqual(result["base_subtotal"], {"amount": "3.95", "currency": "USD"})
        self.assertEqual(
            [option["disposition"] for option in result["shipping_options"]],
            ["delivery", "pickup", "paid_later", "unavailable"],
        )
        self.assertEqual(
            result["rates"],
            [
                {
                    "option_id": "freeshipping/freeshipping",
                    "title": "Free Shipping — Free Shipping",
                    "amount": {"amount": "0", "currency": "USD"},
                }
            ],
        )
        self.assertIn(("GET", f"/rest/V1/guest-carts/{token}/totals"), calls)
        self.assertEqual(calls.count(("POST", "/rest/V1/guest-carts")), 1)
        self.assertNotIn(token, json.dumps(http.evidence))

    def test_add_item_requires_the_requested_quantity(self) -> None:
        regular_handler, _, _ = self.handler()

        def invalid_handler(quantity: Any):
            def handle(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/items"):
                    added = {
                        "sku": "BRG-STEEL",
                        "name": "Configurable Bearing - Steel",
                        "price": 3.95,
                    }
                    if quantity is not None:
                        added["qty"] = quantity
                    return response(request, json_value=added)
                return regular_handler(request)

            return handle

        invalid_quantities = [None, True, 2]
        for quantity in invalid_quantities:
            with self.subTest(quantity=quantity):
                http = Http(httpx.MockTransport(invalid_handler(quantity)))
                with (
                    http.client,
                    self.assertRaisesRegex(ToolError, "requested quantity"),
                ):
                    adapter().quote(
                        http,
                        detection(),
                        [{"ref": magento.item_ref("magento", {"sku": "BRG-STEEL"}), "quantity": 1}],
                        DEFAULT_DESTINATION,
                    )

    def test_empty_rate_array_is_not_free_shipping(self) -> None:
        handler, _, _ = self.handler(fixture_json("platform-magento-empty.json"))
        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = adapter().quote(
                http,
                detection(),
                [{"ref": magento.item_ref("magento", {"sku": "BRG-STEEL"}), "quantity": 1}],
                DEFAULT_DESTINATION,
            )

        self.assertEqual(result["kind"], "empty")
        self.assertEqual(result["reason"], "empty_rate_list")
        self.assertEqual(result["rates"], [])
        self.assertEqual(result["subtotal"]["currency"], "USD")

    def test_noncomparable_options_are_preserved_but_not_quoted(self) -> None:
        rates = fixture_json("platform-magento-rates.json")[1:]
        handler, _, _ = self.handler(rates)
        http = Http(httpx.MockTransport(handler))
        with http.client:
            result = adapter().quote(
                http,
                detection(),
                [{"ref": magento.item_ref("magento", {"sku": "BRG-STEEL"}), "quantity": 1}],
                DEFAULT_DESTINATION,
            )

        self.assertEqual(result["kind"], "empty")
        self.assertEqual(result["reason"], "no_comparable_delivery_rate")
        self.assertEqual(len(result["shipping_options"]), 3)

    def test_rate_available_requires_an_exact_boolean(self) -> None:
        for value in (None, "false", 0):
            with self.subTest(value=value):
                rate = fixture_json("platform-magento-rates.json")[0]
                rate["available"] = value
                handler, _, _ = self.handler([rate])
                http = Http(httpx.MockTransport(handler))
                with (
                    http.client,
                    self.assertRaisesRegex(ToolError, "available must be a boolean"),
                ):
                    adapter().quote(
                        http,
                        detection(),
                        [{"ref": magento.item_ref("magento", {"sku": "BRG-STEEL"}), "quantity": 1}],
                        DEFAULT_DESTINATION,
                    )

        missing = fixture_json("platform-magento-rates.json")[0]
        missing.pop("available")
        handler, _, _ = self.handler([missing])
        http = Http(httpx.MockTransport(handler))
        with (
            http.client,
            self.assertRaisesRegex(ToolError, "available must be a boolean"),
        ):
            adapter().quote(
                http,
                detection(),
                [{"ref": magento.item_ref("magento", {"sku": "BRG-STEEL"}), "quantity": 1}],
                DEFAULT_DESTINATION,
            )

    def test_explicitly_unavailable_rate_stays_unavailable_without_an_error(
        self,
    ) -> None:
        rate = fixture_json("platform-magento-rates.json")[0]
        rate["available"] = False
        rate["error_message"] = ""
        handler, _, _ = self.handler([rate])
        http = Http(httpx.MockTransport(handler))

        with http.client:
            result = adapter().quote(
                http,
                detection(),
                [{"ref": magento.item_ref("magento", {"sku": "BRG-STEEL"}), "quantity": 1}],
                DEFAULT_DESTINATION,
            )

        self.assertEqual(result["kind"], "empty")
        self.assertEqual(result["rates"], [])
        self.assertEqual(result["shipping_options"][0]["disposition"], "unavailable")

    def test_guest_cart_denial_is_gated_and_challenge_is_bot_wall(self) -> None:
        def denial(response_headers: dict[str, str], response_content: bytes):
            def handle(request: httpx.Request) -> httpx.Response:
                return response(
                    request, 403, content=response_content, headers=response_headers
                )

            return handle

        cases = [
            ({}, b"Forbidden", "gated"),
            ({"cf-mitigated": "challenge"}, b"Just a moment", "bot_wall"),
        ]
        for headers, content, expected in cases:
            with self.subTest(expected=expected):
                http = Http(httpx.MockTransport(denial(headers, content)))
                with http.client:
                    result = adapter().quote(
                        http,
                        detection(),
                        [{"ref": magento.item_ref("magento", {"sku": "BRG-STEEL"}), "quantity": 1}],
                        DEFAULT_DESTINATION,
                    )
                self.assertEqual(result["status"], "api_error")
                if expected == "bot_wall":
                    self.assertEqual(result["state"], "bot_wall")

    def test_reference_must_be_exactly_one_sku(self) -> None:
        http = Http(
            httpx.MockTransport(lambda request: self.fail("request must not run"))
        )
        with (
            http.client,
            self.assertRaisesRegex(ToolError, "exactly one nonempty simple SKU"),
        ):
            adapter().quote(
                http,
                detection(),
                [{"ref": magento.item_ref("magento", {"sku": "BRG-STEEL", "currency": "USD"}), "quantity": 1}],
                DEFAULT_DESTINATION,
            )


if __name__ == "__main__":
    unittest.main()
