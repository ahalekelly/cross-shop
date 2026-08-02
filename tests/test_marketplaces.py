from __future__ import annotations

import json

import httpx
import pytest

from storefront.adapters.marketplaces import Ebay, SerpApi, ShopifyGlobal
from storefront.core import DetectedStore, Session, ToolError

DESTINATION = {"country": "US", "region": "CA", "city": "San Francisco", "address1": "747 Howard St", "postal_code": "94103"}


def detection(platform: str, origin: str) -> DetectedStore:
    return DetectedStore(origin=origin, entry_url=origin + "/", platform=platform, api_origin=origin, evidence=("pseudo",))


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
            assert "country=US,zip=94103" in request.headers["X-EBAY-C-ENDUSERCTX"]
            return httpx.Response(200, json={"itemSummaries": [{"itemId": "v1|1|0", "title": "Valve", "itemWebUrl": "https://www.ebay.com/itm/1", "price": {"value": "9.99", "currency": "USD"}}]}, request=request)
        raise AssertionError(request.url)

    adapter = Ebay({"ebay": {"client_id": "id", "client_secret": "secret"}})
    session = Session(httpx.MockTransport(handler))
    adapter.search(session, detection("ebay", "https://www.ebay.com"), "valve", 20, DESTINATION)
    adapter.search(session, detection("ebay", "https://www.ebay.com"), "valve", 20, DESTINATION)
    assert sum(request.url.path == "/identity/v1/oauth2/token" for request in requests) == 1


def test_ebay_missing_credentials_is_specific() -> None:
    with pytest.raises(ToolError, match="settings.ebay"):
        Ebay({}).search(Session(httpx.MockTransport(lambda request: None)), detection("ebay", "https://www.ebay.com"), "valve", 20, DESTINATION)


def test_shopify_global_uses_current_ucp_contract() -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"products": [{"id": "gid://shopify/p/abc", "title": "Boot", "price_range": {"min": "40", "currency": "USD"}, "offers": [{"url": "https://merchant.test/products/boot"}]}]}}}, request=request)

    adapter = ShopifyGlobal({"shopify_global": {"profile_url": "https://agent.test/profile.json"}})
    result = adapter.search(Session(httpx.MockTransport(handler)), detection("shopify_global", "https://shop.app"), "boot", 10, DESTINATION)
    assert bodies[0]["params"]["name"] == "search_catalog"
    assert bodies[0]["params"]["_meta"]["ucp-agent"]["profile"] == "https://agent.test/profile.json"
    assert result["items"][0]["item_ref"]["product_id"] == "gid://shopify/p/abc"


def test_shopify_global_requires_profile_url() -> None:
    with pytest.raises(ToolError, match="profile_url"):
        ShopifyGlobal({}).search(Session(httpx.MockTransport(lambda request: None)), detection("shopify_global", "https://shop.app"), "boot", 10, DESTINATION)
