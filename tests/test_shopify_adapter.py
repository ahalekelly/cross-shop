from __future__ import annotations

import json

import httpx
import pytest

from storefront.adapters import shopify
from storefront.core import DEFAULT_DESTINATION, DetectedStore, Session, ToolError, destination_for, item_ref


def detection() -> DetectedStore:
    return DetectedStore(
        origin="https://store.example",
        entry_url="https://store.example/",
        platform="shopify",
        api_origin="https://backend.myshopify.com",
        evidence=("test",),
    )


def test_detection_discovers_myshopify_backend() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "store.example":
            return httpx.Response(404, request=request)
        assert request.url.host == "main-us-attitude.myshopify.com"
        return httpx.Response(200, json={"data": {"shop": {"name": "ATTITUDE"}}}, request=request)

    homepage_request = httpx.Request("GET", "https://store.example/")
    homepage = httpx.Response(200, text='{"publicStoreDomain":"main-us-attitude.myshopify.com"}', request=homepage_request)
    result = shopify.detect(
        Session(httpx.MockTransport(handler)),
        "https://store.example",
        "https://store.example/",
        homepage,
    )
    assert result is not None
    assert result.api_origin == "https://main-us-attitude.myshopify.com"


def test_detection_rejects_cross_authority_redirect() -> None:
    homepage_request = httpx.Request("GET", "https://store.example/")
    homepage = httpx.Response(200, text="<html></html>", request=homepage_request)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": "https://attacker.test/graphql"}, request=request)

    assert shopify.detect(
        Session(httpx.MockTransport(handler)),
        "https://store.example",
        "https://store.example/",
        homepage,
    ) is None


def test_search_flattens_exact_variants() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["variables"] == {"query": "VALVE-1"}
        return httpx.Response(200, json={"data": {"products": {"nodes": [{"title": "Valve", "handle": "valve", "description": "Live", "images": {"nodes": [{"url": "https://cdn.test/valve.jpg"}]}, "variants": {"nodes": [{"id": "gid://shopify/ProductVariant/7", "title": "1 inch", "sku": "VALVE-1", "availableForSale": True, "selectedOptions": [{"name": "Size", "value": "1 inch"}], "price": {"amount": "12.50", "currencyCode": "USD"}, "compareAtPrice": None}]}}]}}}, request=request)

    result = shopify.search(Session(httpx.MockTransport(handler)), detection(), "VALVE-1")
    assert result["items"][0]["sku"] == "VALVE-1"
    assert result["items"][0]["price"] == {"amount": "12.50", "currency": "USD"}
    assert result["items"][0]["image_urls"] == ["https://cdn.test/valve.jpg"]


@pytest.mark.parametrize("errors", [None, {}, [], ["bad"], [{}], [{"message": ""}]])
def test_graphql_error_shape_is_strict(errors: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": errors}, request=request)

    with pytest.raises(ToolError, match="GraphQL errors"):
        shopify.search(Session(httpx.MockTransport(handler)), detection(), "bearing")


def test_graphql_error_messages_are_preserved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "first"}, {"message": "second"}]}, request=request)

    with pytest.raises(ToolError, match="first; second"):
        shopify.search(Session(httpx.MockTransport(handler)), detection(), "bearing")


def test_quote_parses_deferred_multipart_and_preserves_pickup() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if "cartCreate" in body["query"]:
            address = body["variables"]["input"]["buyerIdentity"]["deliveryAddressPreferences"][0]["deliveryAddress"]
            assert address == destination_for("shopify", DEFAULT_DESTINATION)
            return httpx.Response(200, json={"data": {"cartCreate": {"cart": {"id": "gid://shopify/Cart/secret", "cost": {"subtotalAmount": {"amount": "12.50", "currencyCode": "USD"}}}, "userErrors": []}}}, request=request)
        payload = (
            b"--graphql\r\nContent-Type: application/json\r\n\r\n"
            b'{"data":{"cart":{"id":"secret"}},"hasNext":true}\r\n'
            b"--graphql\r\nContent-Type: application/json\r\n\r\n"
            b'{"incremental":[{"path":["cart"],"data":{"deliveryGroups":{"nodes":[{"deliveryOptions":[{"handle":"ground","title":"Ground","code":"GROUND","description":null,"deliveryMethodType":"SHIPPING","estimatedCost":{"amount":"6.95","currencyCode":"USD"}},{"handle":null,"title":"Pickup","code":null,"description":null,"deliveryMethodType":"PICK_UP","estimatedCost":{"amount":"0","currencyCode":"USD"}}]}]}}}],"hasNext":false}\r\n'
            b"--graphql--\r\n"
        )
        return httpx.Response(200, headers={"content-type": 'multipart/mixed; boundary="graphql"'}, content=payload, request=request)

    result = shopify.quote_many(
        Session(httpx.MockTransport(handler)),
        detection(),
        [{"ref": item_ref("shopify", {"variant_id": "gid://shopify/ProductVariant/7"}), "quantity": 2}],
        DEFAULT_DESTINATION,
    )
    assert calls == 2
    assert [option["disposition"] for option in result["shipping_options"]] == ["delivery", "pickup"]
    assert [rate["option_id"] for rate in result["rates"]] == ["ground"]


def test_signed_redirect_rebuilds_and_resigns_request() -> None:
    requests: list[httpx.Request] = []
    signatures = 0

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(307, headers={"location": "/api/2026-07/graphql.json"}, request=request)
        return httpx.Response(200, json={"data": {"products": {"nodes": []}}}, request=request)

    def signer(client: httpx.Client, request: httpx.Request) -> httpx.Response:
        nonlocal signatures
        signatures += 1
        assert "Signature-Agent" not in request.headers
        request.headers["Signature-Agent"] = f'"call-{signatures}"'
        return client.send(request, follow_redirects=False)

    result = shopify.search(Session(httpx.MockTransport(handler), signer), detection(), "bearing")
    assert result["items"] == []
    assert signatures == 2
    assert requests[0].headers["Signature-Agent"] != requests[1].headers["Signature-Agent"]


@pytest.mark.parametrize("method, disposition", [("SHIPPING", "delivery"), ("LOCAL", "delivery"), ("PICK_UP", "pickup"), ("PICKUP_POINT", "pickup"), ("RETAIL", "pickup"), ("NONE", "unavailable")])
def test_shipping_method_dispositions(method: str, disposition: str) -> None:
    option = {"handle": "ground", "title": "Ground", "code": "GROUND", "description": None, "deliveryMethodType": method, "estimatedCost": {"amount": "6.95", "currencyCode": "USD"}}
    assert shopify._shipping_option(option)["disposition"] == disposition


def test_empty_rate_list_is_not_free_shipping() -> None:
    responses = iter([
        {"data": {"cartCreate": {"cart": {"id": "gid://shopify/Cart/secret", "cost": {"subtotalAmount": {"amount": "9.99", "currencyCode": "USD"}}}, "userErrors": []}}},
        {"data": {"cart": {"deliveryGroups": {"nodes": []}}}},
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses), request=request)

    result = shopify.quote_many(
        Session(httpx.MockTransport(handler)),
        detection(),
        [{"ref": item_ref("shopify", {"variant_id": "gid://shopify/ProductVariant/7"}), "quantity": 1}],
        DEFAULT_DESTINATION,
    )
    assert result["kind"] == "empty"
    assert result["rates"] == []
