from __future__ import annotations

import json

import httpx
import pytest

from storefront.adapters import bigcommerce, magento, shopify, squarespace, woocommerce
from storefront.core import (
    DetectedStore,
    MagentoDetectedStore,
    Session,
    ToolError,
    WooCommerceQuote,
    WooCommerceShipping,
    money,
    quote_outcome,
    shipping_option,
)

DESTINATION = {"country": "US", "region": "CA", "city": "San Francisco", "address1": "747 Howard St", "postal_code": "94103"}


def detected(platform: str) -> DetectedStore:
    return DetectedStore(origin="https://store.test", entry_url="https://store.test/", platform=platform, api_origin="https://store.test", evidence=("test",))


def test_fallback_shipping_is_not_reported_as_empty_or_delivery() -> None:
    option = shipping_option(
        WooCommerceShipping(selected=True, tax=money(0, "USD")),
        "carrier_fallback",
        "Carrier fallback",
        "fallback",
        money(10, "USD"),
    )
    result = quote_outcome(
        WooCommerceQuote(cart_totals={}, cleanup_status=204),
        [option],
        money(25, "USD"),
    )
    assert result["kind"] == "fallback"
    assert result["fallback_rate_ids"] == ["carrier_fallback"]
    assert result["rates"] == []


def test_shopify_quote_uses_one_cart_with_all_quantities() -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "mutation CartCreate" in body["query"]:
            return httpx.Response(200, json={"data": {"cartCreate": {"cart": {"id": "gid://shopify/Cart/1", "cost": {"subtotalAmount": {"amount": "30", "currencyCode": "USD"}}}, "userErrors": []}}}, request=request)
        return httpx.Response(200, json={"data": {"cart": {"deliveryGroups": {"nodes": [{"deliveryOptions": [{"handle": "ground", "title": "Ground", "deliveryMethodType": "SHIPPING", "estimatedCost": {"amount": "5", "currencyCode": "USD"}}]}]}}}}, request=request)

    lines = [
        {"ref": {"platform": "shopify", "store": "https://store.test", "variant_id": "gid://shopify/ProductVariant/1"}, "quantity": 2},
        {"ref": {"platform": "shopify", "store": "https://store.test", "variant_id": "gid://shopify/ProductVariant/2"}, "quantity": 3},
    ]
    result = shopify.Shopify({}).quote(Session(httpx.MockTransport(handler)), detected("shopify"), lines, DESTINATION)
    assert [line["quantity"] for line in bodies[0]["variables"]["input"]["lines"]] == [2, 3]
    assert bodies[0]["variables"]["input"]["buyerIdentity"]["deliveryAddressPreferences"][0]["deliveryAddress"]["zip"] == "94103"
    assert result["kind"] == "quote"


def test_woocommerce_quote_reuses_token_and_cleans_every_line() -> None:
    added = 0
    deleted = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal added
        if request.method == "GET":
            return httpx.Response(200, json={"totals": {"currency_code": "USD", "currency_minor_unit": 2, "total_items": "0"}}, headers={"Cart-Token": "token"}, request=request)
        if request.url.path.endswith("/add-item"):
            added += 1
            body = json.loads(request.content)
            return httpx.Response(200, json={"items": [{"id": body["id"], "key": f"key-{body['id']}"}]}, request=request)
        if request.url.path.endswith("/update-customer"):
            return httpx.Response(200, json={"totals": {"currency_code": "USD", "currency_minor_unit": 2, "total_items": "2500", "total_price": "3000"}, "shipping_rates": [{"shipping_rates": [{"rate_id": "ground", "name": "Ground", "price": "500", "taxes": "0", "currency_code": "USD", "currency_minor_unit": 2, "selected": False}]}]}, request=request)
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(204, request=request)
        raise AssertionError(request.url)

    lines = [
        {"ref": {"platform": "woocommerce", "store": "https://store.test", "product_id": 1, "product_type": "simple", "minimum": 1}, "quantity": 2},
        {"ref": {"platform": "woocommerce", "store": "https://store.test", "product_id": 2, "product_type": "variation", "minimum": 1}, "quantity": 1},
    ]
    result = woocommerce.WooCommerce().quote(Session(httpx.MockTransport(handler)), detected("woocommerce"), lines, DESTINATION)
    assert added == 2
    assert len(deleted) == 2
    assert result["subtotal"]["amount"] == "25.00"


def test_magento_quote_adds_all_lines_to_one_guest_cart() -> None:
    quantities = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/V1/guest-carts":
            return httpx.Response(200, json="1234567890abcdef", request=request)
        if request.url.path.endswith("/items"):
            body = json.loads(request.content)["cartItem"]
            quantities.append(body["qty"])
            return httpx.Response(200, json={"sku": body["sku"], "qty": body["qty"], "name": body["sku"]}, request=request)
        if request.url.path.endswith("/totals"):
            return httpx.Response(200, json={"quote_currency_code": "USD", "base_currency_code": "USD", "subtotal": 40}, request=request)
        if request.url.path.endswith("/estimate-shipping-methods"):
            return httpx.Response(200, json=[{"carrier_code": "ups", "method_code": "ground", "carrier_title": "UPS", "method_title": "Ground", "available": True, "amount": 8}], request=request)
        raise AssertionError(request.url)

    detection = MagentoDetectedStore(origin="https://store.test", entry_url="https://store.test/", api_origin="https://store.test", evidence=("test",), search_source="graphql")
    lines = [
        {"ref": {"platform": "magento", "store": "https://store.test", "sku": "A"}, "quantity": 2},
        {"ref": {"platform": "magento", "store": "https://store.test", "sku": "B"}, "quantity": 4},
    ]
    result = magento.Magento().quote(Session(httpx.MockTransport(handler)), detection, lines, DESTINATION)
    assert quantities == [2, 4]
    assert result["kind"] == "quote"


def test_bigcommerce_quote_sends_all_line_items() -> None:
    cart_body = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cart_body
        if request.method == "GET":
            product_id = 1 if request.url.path == "/a" else 2
            html = (
                f'<h1>Product {product_id}</h1><input name="product_id" value="{product_id}">'
                '<script>var BCData = {"product_attributes":{"instock":true,"purchasable":true}};</script>'
            )
            return httpx.Response(200, text=html, request=request)
        if request.url.path == "/api/storefront/carts":
            cart_body = json.loads(request.content)
            return httpx.Response(200, headers=[("set-cookie", "SHOP_SESSION_TOKEN=session; Path=/"), ("set-cookie", "SF-CSRF-TOKEN=csrf; Path=/")], json={"id": "cart", "baseAmount": 30, "currency": {"code": "USD"}, "lineItems": {"physicalItems": [{"id": "line1", "productId": 1, "isShippingRequired": True}, {"id": "line2", "productId": 2, "isShippingRequired": True}]}}, request=request)
        return httpx.Response(200, json={"consignments": [{"availableShippingOptions": [{"id": "ground", "description": "Ground", "cost": 5}]}]}, request=request)

    lines = [
        {"ref": {"platform": "bigcommerce", "store": "https://store.test", "product_id": 1, "product_url": "https://store.test/a"}, "quantity": 2},
        {"ref": {"platform": "bigcommerce", "store": "https://store.test", "product_id": 2, "product_url": "https://store.test/b"}, "quantity": 1},
    ]
    result = bigcommerce.BigCommerce().quote(Session(httpx.MockTransport(handler)), detected("bigcommerce"), lines, DESTINATION)
    assert [item["quantity"] for item in cart_body["lineItems"]] == [2, 1]
    assert result["kind"] == "quote"


def test_squarespace_quote_repeats_adds_then_fetches_shipping_once() -> None:
    adds = []
    shipping = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal shipping
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "website": {"id": "site"},
                    "collection": {"type": 13, "fullUrl": "/shop"},
                    "items": [
                        {"id": "1", "variants": [{"sku": "A", "unlimited": True}]},
                        {"id": "2", "variants": [{"sku": "B", "qtyInStock": 1}]},
                    ],
                },
                headers={"set-cookie": "crumb=token; Path=/"},
                request=request,
            )
        if request.url.path.endswith("/shopping-cart/entries"):
            body = json.loads(request.content)
            adds.append(body["quantity"])
            entry = {"itemId": body["itemId"], "chosenVariant": {"sku": body["sku"]}, "quantity": body["quantity"]}
            return httpx.Response(200, json={"newlyAdded": entry, "shoppingCart": {"cartToken": "cart", "entries": [entry]}}, request=request)
        shipping += 1
        return httpx.Response(200, json={"shippingOptionsStatus": "APPLICABLE_SHIPPING_OPTIONS", "subtotal": {"decimalValue": "30", "currencyCode": "USD"}, "fulfillmentOptions": [{"key": "ground", "name": "Ground", "isPickup": False, "price": {"decimalValue": "5", "currencyCode": "USD"}}]}, request=request)

    lines = [
        {"ref": {"platform": "squarespace", "store": "https://store.test", "collection_url": "https://store.test/shop", "item_id": "1", "sku": "A"}, "quantity": 2},
        {"ref": {"platform": "squarespace", "store": "https://store.test", "collection_url": "https://store.test/shop", "item_id": "2", "sku": "B"}, "quantity": 1},
    ]
    result = squarespace.Squarespace().quote(Session(httpx.MockTransport(handler)), detected("squarespace"), lines, DESTINATION)
    assert adds == [2, 1]
    assert shipping == 1
    assert result["kind"] == "quote"


def test_woocommerce_failed_second_add_cleans_first_line() -> None:
    added = 0
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal added
        if request.method == "GET":
            return httpx.Response(200, json={"totals": {"currency_code": "USD", "currency_minor_unit": 2, "total_items": "0"}}, headers={"Cart-Token": "token"}, request=request)
        if request.url.path.endswith("/add-item"):
            added += 1
            if added == 2:
                return httpx.Response(500, request=request)
            body = json.loads(request.content)
            return httpx.Response(200, json={"items": [{"id": body["id"], "key": "first-key"}]}, request=request)
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(204, request=request)
        raise AssertionError(request.url)

    lines = [
        {"ref": {"platform": "woocommerce", "store": "https://store.test", "product_id": 1, "product_type": "simple", "minimum": 1}, "quantity": 1},
        {"ref": {"platform": "woocommerce", "store": "https://store.test", "product_id": 2, "product_type": "simple", "minimum": 1}, "quantity": 1},
    ]
    with pytest.raises(ToolError, match="HTTP 500"):
        woocommerce.WooCommerce().quote(
            Session(httpx.MockTransport(handler)),
            detected("woocommerce"),
            lines,
            DESTINATION,
        )

    assert deleted == ["/wp-json/wc/store/v1/cart/items/first-key"]


def test_squarespace_rejects_cart_token_changes_between_adds() -> None:
    adds = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal adds
        if request.method == "GET":
            return httpx.Response(200, json={"website": {"id": "site"}, "collection": {"type": 13, "fullUrl": "/shop"}, "items": [{"id": "1", "variants": [{"sku": "A", "unlimited": True}]}, {"id": "2", "variants": [{"sku": "B", "unlimited": True}]}]}, headers={"set-cookie": "crumb=token; Path=/"}, request=request)
        if request.url.path.endswith("/shopping-cart/entries"):
            adds += 1
            body = json.loads(request.content)
            entry = {"itemId": body["itemId"], "chosenVariant": {"sku": body["sku"]}, "quantity": body["quantity"]}
            return httpx.Response(200, json={"newlyAdded": entry, "shoppingCart": {"cartToken": f"cart-{adds}", "entries": [entry]}}, request=request)
        raise AssertionError(request.url)

    lines = [
        {"ref": {"platform": "squarespace", "store": "https://store.test", "collection_url": "https://store.test/shop", "item_id": "1", "sku": "A"}, "quantity": 1},
        {"ref": {"platform": "squarespace", "store": "https://store.test", "collection_url": "https://store.test/shop", "item_id": "2", "sku": "B"}, "quantity": 1},
    ]
    with pytest.raises(ToolError, match="token changed"):
        squarespace.Squarespace().quote(
            Session(httpx.MockTransport(handler)),
            detected("squarespace"),
            lines,
            DESTINATION,
        )
