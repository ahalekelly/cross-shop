from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest

from storefront.adapters.marketplaces import AliExpress, Ebay, SerpApi, ShopifyGlobal
from storefront.core import DetectedStore, Session, ToolError
from storefront.service import Storefront
from storefront.storage import DataStore

FIXTURES = Path(__file__).parent / "fixtures"

DESTINATION = {"country": "US", "region": "CA", "city": "San Francisco", "address1": "747 Howard St", "postal_code": "94103"}


def detection(platform: str, origin: str) -> DetectedStore:
    return DetectedStore(origin=origin, entry_url=origin + "/", platform=platform, api_origin=origin, evidence=("pseudo",))


def test_serpapi_google_shopping_maps_recorded_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "secret")
    recorded = json.loads((FIXTURES / "aggregator-serpapi-malformed.json").read_text())["shopping_result"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["engine"] == "google_shopping"
        return httpx.Response(200, json={"search_metadata": {"status": "Success"}, "shopping_results": [recorded]}, request=request)

    result = SerpApi("google_shopping").search(
        Session(httpx.MockTransport(handler)),
        detection("google_shopping", "https://shopping.google.com"),
        "pliers",
        20,
        DESTINATION,
    )
    assert result["items"][0]["product_url"] == "https://retailer.example/product/1"
    assert result["items"][0]["lead"] is True


def test_serpapi_rejects_ambiguous_and_malformed_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "secret")
    fixture_data = json.loads((FIXTURES / "aggregator-serpapi-malformed.json").read_text())
    payloads = [
        {"search_metadata": {"status": "Success"}, "shopping_results": [], "inline_shopping_results": []},
        {"search_metadata": {"status": "Success"}, "shopping_results": [fixture_data["malformed"]]},
    ]

    for payload in payloads:
        def handler(request: httpx.Request, payload=payload) -> httpx.Response:
            return httpx.Response(200, json=payload, request=request)

        with pytest.raises(ToolError):
            SerpApi("google_shopping").search(
                Session(httpx.MockTransport(handler)),
                detection("google_shopping", "https://shopping.google.com"),
                "pliers",
                20,
                DESTINATION,
            )


def test_aliexpress_maps_recorded_affiliate_product(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "key")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "secret")
    product = json.loads((FIXTURES / "aggregator-aliexpress-malformed.json").read_text())["product"]
    payload = {"aliexpress_affiliate_product_query_response": {"resp_result": {"resp_code": 200, "result": {"products": {"product": [product]}}}}}

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(parse_qsl(request.content.decode()))
        signature = params.pop("sign")
        message = "".join(key + params[key] for key in sorted(params))
        expected = hmac.new(b"secret", message.encode(), hashlib.md5).hexdigest().upper()
        assert signature == expected
        return httpx.Response(200, json=payload, request=request)

    result = AliExpress().search(
        Session(httpx.MockTransport(handler)),
        detection("aliexpress", "https://www.aliexpress.com"),
        "inserts",
        20,
        DESTINATION,
    )
    assert result["items"][0]["price"] == {"amount": "11.20", "currency": "USD"}
    assert result["items"][0]["lead"] is True


def test_aliexpress_rejects_recorded_malformed_product(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "key")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "secret")
    product = json.loads((FIXTURES / "aggregator-aliexpress-malformed.json").read_text())["malformed"]
    payload = {"aliexpress_affiliate_product_query_response": {"resp_result": {"resp_code": 200, "result": {"products": {"product": [product]}}}}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    with pytest.raises(ToolError):
        AliExpress().search(
            Session(httpx.MockTransport(handler)),
            detection("aliexpress", "https://www.aliexpress.com"),
            "inserts",
            20,
            DESTINATION,
        )


def test_serpapi_amazon_maps_engine_and_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "secret")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["engine"] == "amazon"
        assert request.url.params["k"] == "coffee"
        return httpx.Response(200, json={"search_metadata": {"status": "Success"}, "organic_results": [{"asin": "B123", "title": "Coffee", "link_clean": "https://www.amazon.com/dp/B123/", "extracted_price": 12.5, "thumbnail": "https://images.test/a.jpg"}]}, request=request)

    session = Session(httpx.MockTransport(handler))
    result = SerpApi("amazon").search(session, detection("amazon", "https://www.amazon.com"), "coffee", 20, DESTINATION)
    assert result["items"][0]["item_ref"] == {"platform": "amazon", "asin": "B123", "merchant_url": "https://www.amazon.com/dp/B123/"}


def test_ebay_mints_one_token_and_includes_contextual_location() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/identity/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200}, request=request)
        if request.url.path.endswith("/item_summary/search"):
            assert request.headers["X-EBAY-C-ENDUSERCTX"] == "contextualLocation=country%3DUS,zip%3D94103"
            return httpx.Response(200, json={"itemSummaries": [{"itemId": "v1|1|0", "title": "Valve", "itemWebUrl": "https://www.ebay.com/itm/1", "price": {"value": "9.99", "currency": "USD"}}]}, request=request)
        raise AssertionError(request.url)

    adapter = Ebay({"ebay": {"client_id": "id", "client_secret": "secret"}})
    session = Session(httpx.MockTransport(handler))
    adapter.search(session, detection("ebay", "https://www.ebay.com"), "valve", 20, DESTINATION)
    adapter.search(session, detection("ebay", "https://www.ebay.com"), "valve", 20, DESTINATION)
    assert sum(request.url.path == "/identity/v1/oauth2/token" for request in requests) == 1


def test_ebay_product_url_uses_legacy_id_lookup_and_returns_shipping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/identity/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token"}, request=request)
        assert request.url.path.endswith("/item/get_item_by_legacy_id")
        assert request.url.params["legacy_item_id"] == "123456789012"
        return httpx.Response(200, json={"itemId": "v1|123456789012|0", "title": "Valve", "itemWebUrl": "https://www.ebay.com/itm/123456789012", "price": {"value": "9.99", "currency": "USD"}, "shippingOptions": [{"shippingCost": {"value": "5.00", "currency": "USD"}}]}, request=request)

    result = Ebay({"ebay": {"client_id": "id", "client_secret": "secret"}}).product(
        Session(httpx.MockTransport(handler)),
        detection("ebay", "https://www.ebay.com"),
        {"url": "https://www.ebay.com/itm/123456789012"},
        DESTINATION,
    )

    assert result["shipping_options"][0]["shippingCost"]["value"] == "5.00"


def test_ebay_missing_credentials_is_specific() -> None:
    with pytest.raises(ToolError, match="settings.ebay"):
        Ebay({}).search(Session(httpx.MockTransport(lambda request: None)), detection("ebay", "https://www.ebay.com"), "valve", 20, DESTINATION)


def test_shopify_global_uses_current_ucp_contract() -> None:
    bodies = []
    payload = json.loads((FIXTURES / "marketplace-shopify-global-search.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        assert str(request.url) == "https://catalog.shopify.com/api/ucp/mcp"
        return httpx.Response(200, json=payload, request=request)

    adapter = ShopifyGlobal({"shopify_global": {"profile_url": "https://agent.test/profile.json"}})
    result = adapter.search(Session(httpx.MockTransport(handler)), detection("shopify_global", "https://shop.app"), "boot", 10, DESTINATION)
    arguments = bodies[0]["params"]["arguments"]
    assert bodies[0]["params"]["name"] == "search_catalog"
    assert arguments["meta"]["ucp-agent"]["profile"] == "https://agent.test/profile.json"
    assert arguments["catalog"]["filters"]["ships_to"] == {"country": "US", "region": "CA", "postal_code": "94103"}
    assert result["items"][0]["price"] == {"amount": "40.00", "currency": "USD"}
    assert result["items"][0]["item_ref"] == {"platform": "shopify_global", "product_id": "gid://shopify/p/abc", "variant_id": "gid://shopify/ProductVariant/1"}
    assert result["items"][0]["seller_url"] == "https://merchant.test"
    assert result["items"][0]["seller_domain"] == "merchant-api.myshopify.com"
    assert result["items"][0]["variant_url"].endswith("?variant=1")
    assert result["items"][0]["checkout_url"] == "https://merchant.test/cart/1:1"


def test_shopify_global_search_writes_handles_and_shopify_merchants(tmp_path: Path) -> None:
    data = DataStore(tmp_path)
    data.settings_path.write_text(json.dumps({"shopify_global": {"profile_url": "https://agent.test/profile.json"}}))
    payload = json.loads((FIXTURES / "marketplace-shopify-global-search.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    result = Storefront(data, lambda origin: httpx.MockTransport(handler)).search(
        [{"store": "https://shop.app", "query": "boot"}]
    )

    assert result["run"] == "r1"
    assert "currency" not in result["stores"][0]
    assert [item["currency"] for item in result["stores"][0]["items"]] == ["USD", "EUR"]
    assert [item["price"] for item in result["stores"][0]["items"]] == [40, 45]
    cached = data.resolve_handle("r1.1.1")
    assert cached["image_urls"] == ["https://cdn.shopify.com/trail-boot.jpg"]
    merchant = data.vendor("https://merchant.test")
    assert merchant["evidence"] == ["global_catalog_result"]
    assert merchant["api_origin"] == "https://merchant-api.myshopify.com"
    assert data.vendor("https://other-merchant.test")["api_origin"] == "https://other-api.myshopify.com"


def test_shopify_global_product_uses_get_product_ucp_shape() -> None:
    search_payload = json.loads((FIXTURES / "marketplace-shopify-global-search.json").read_text())
    product = search_payload["result"]["structuredContent"]["products"][0]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["params"]["name"] == "get_product"
        assert body["params"]["arguments"]["catalog"]["id"] == "gid://shopify/p/abc"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"product": product}}}, request=request)

    result = ShopifyGlobal({"shopify_global": {"profile_url": "https://agent.test/profile.json"}}).product(
        Session(httpx.MockTransport(handler)),
        detection("shopify_global", "https://shop.app"),
        {"ref": {"platform": "shopify_global", "store": "https://shop.app", "product_id": "gid://shopify/p/abc"}},
        DESTINATION,
    )

    assert result["kind"] == "search"
    assert [item["variant"] for item in result["items"]] == ["Black / 10", "Brown / 11"]


def test_shopify_global_ucp_application_error_is_not_empty_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"ucp": {"status": "error"}, "messages": [{"content": "profile rejected"}]}}}, request=request)

    result = ShopifyGlobal({"shopify_global": {"profile_url": "https://agent.test/profile.json"}}).search(
        Session(httpx.MockTransport(handler)),
        detection("shopify_global", "https://shop.app"),
        "boot",
        10,
        DESTINATION,
    )

    assert result["status"] == "api_error"
    assert "profile rejected" in result["reason"]


def test_aliexpress_checks_application_response_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "key")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "secret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"aliexpress_affiliate_product_query_response": {"resp_result": {"resp_code": 500, "resp_msg": "failed"}}}, request=request)

    result = AliExpress().search(
        Session(httpx.MockTransport(handler)),
        detection("aliexpress", "https://www.aliexpress.com"),
        "valve",
        10,
        DESTINATION,
    )
    assert result["status"] == "api_error"
    assert "500" in result["reason"]


def test_serpapi_provider_error_redacts_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "super-secret-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Invalid key super-secret-key"}, request=request)

    result = SerpApi("google_shopping").search(
        Session(httpx.MockTransport(handler)),
        detection("google_shopping", "https://shopping.google.com"),
        "valve",
        10,
        DESTINATION,
    )
    assert result["status"] == "api_error"
    assert "super-secret-key" not in result["reason"]
    assert "[redacted]" in result["reason"]


def test_shopify_global_requires_profile_url() -> None:
    with pytest.raises(ToolError, match="profile_url"):
        ShopifyGlobal({}).search(Session(httpx.MockTransport(lambda request: None)), detection("shopify_global", "https://shop.app"), "boot", 10, DESTINATION)
