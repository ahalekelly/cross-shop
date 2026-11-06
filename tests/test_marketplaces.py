from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest

from cross_shop.adapters.marketplaces import AliExpress, Ebay, SerpApi, ShopifyGlobal
from cross_shop.core import DetectedStore, Session, ToolError
from cross_shop.service import CrossShop
from cross_shop.storage import DataStore

FIXTURES = Path(__file__).parent / "fixtures"

DESTINATION = {"country": "US", "region": "CA", "city": "San Francisco", "address1": "747 Howard St", "postal_code": "94103"}

SETTINGS = {"serpapi": {"api_key": "secret"}, "aliexpress": {"app_key": "key", "app_secret": "secret"}}


def detection(platform: str, origin: str) -> DetectedStore:
    return DetectedStore(origin=origin, entry_url=origin + "/", platform=platform, api_origin=origin, evidence=("pseudo",))


def test_serpapi_google_shopping_maps_recorded_result() -> None:
    recorded = json.loads((FIXTURES / "aggregator-serpapi-malformed.json").read_text())["shopping_result"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["engine"] == "google_shopping"
        return httpx.Response(200, json={"search_metadata": {"status": "Success"}, "shopping_results": [recorded]}, request=request)

    result = SerpApi("google_shopping", SETTINGS).search(
        Session(httpx.MockTransport(handler)),
        detection("google_shopping", "https://shopping.google.com"),
        "pliers",
        20,
        DESTINATION,
    )
    assert result["items"][0]["product_url"] == "https://retailer.example/product/1"
    assert result["items"][0]["lead"] is True


def test_serpapi_rejects_ambiguous_and_malformed_results() -> None:
    fixture_data = json.loads((FIXTURES / "aggregator-serpapi-malformed.json").read_text())
    payloads = [
        {"search_metadata": {"status": "Success"}, "shopping_results": [], "inline_shopping_results": []},
        {"search_metadata": {"status": "Success"}, "shopping_results": [fixture_data["malformed"]]},
    ]

    for payload in payloads:
        def handler(request: httpx.Request, payload=payload) -> httpx.Response:
            return httpx.Response(200, json=payload, request=request)

        with pytest.raises(ToolError):
            SerpApi("google_shopping", SETTINGS).search(
                Session(httpx.MockTransport(handler)),
                detection("google_shopping", "https://shopping.google.com"),
                "pliers",
                20,
                DESTINATION,
            )


def test_aliexpress_maps_recorded_affiliate_product() -> None:
    product = json.loads((FIXTURES / "aggregator-aliexpress-malformed.json").read_text())["product"]
    payload = {"aliexpress_affiliate_product_query_response": {"resp_result": {"resp_code": 200, "result": {"products": {"product": [product]}}}}}

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(parse_qsl(request.content.decode()))
        signature = params.pop("sign")
        message = "".join(key + params[key] for key in sorted(params))
        expected = hmac.new(b"secret", message.encode(), hashlib.md5).hexdigest().upper()
        assert signature == expected
        return httpx.Response(200, json=payload, request=request)

    result = AliExpress(SETTINGS).search(
        Session(httpx.MockTransport(handler)),
        detection("aliexpress", "https://www.aliexpress.com"),
        "inserts",
        20,
        DESTINATION,
    )
    assert result["items"][0]["price"] == {"amount": "11.20", "currency": "USD"}
    assert result["items"][0]["lead"] is True


def test_aliexpress_rejects_recorded_malformed_product() -> None:
    product = json.loads((FIXTURES / "aggregator-aliexpress-malformed.json").read_text())["malformed"]
    payload = {"aliexpress_affiliate_product_query_response": {"resp_result": {"resp_code": 200, "result": {"products": {"product": [product]}}}}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    with pytest.raises(ToolError):
        AliExpress(SETTINGS).search(
            Session(httpx.MockTransport(handler)),
            detection("aliexpress", "https://www.aliexpress.com"),
            "inserts",
            20,
            DESTINATION,
        )


def test_serpapi_amazon_maps_engine_and_results() -> None:

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["engine"] == "amazon"
        assert request.url.params["k"] == "coffee"
        return httpx.Response(200, json={"search_metadata": {"status": "Success"}, "organic_results": [{"asin": "B0CZP3CDSZ", "title": "Coffee", "link_clean": "https://www.amazon.com/dp/B0CZP3CDSZ/", "extracted_price": 12.5, "thumbnail": "https://images.test/a.jpg"}]}, request=request)

    session = Session(httpx.MockTransport(handler))
    result = SerpApi("amazon", SETTINGS).search(session, detection("amazon", "https://www.amazon.com"), "coffee", 20, DESTINATION)
    assert result["items"][0]["item_ref"] == {"platform": "amazon", "asin": "B0CZP3CDSZ"}


def test_serpapi_keeps_results_without_a_featured_offer_price() -> None:
    priced = {"asin": "B0GL7Q7173", "title": "Cube 35 Pint Dehumidifier", "link_clean": "https://www.amazon.com/dp/B0GL7Q7173/", "price": "$209.99", "extracted_price": 209.99}
    see_options = {"asin": "B09155CW3S", "title": "Cube 35 Pint Dehumidifier, White", "link_clean": "https://www.amazon.com/dp/B09155CW3S/", "options": "See options", "purchase_options": [{"no_featured_offers": True}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"search_metadata": {"status": "Success"}, "organic_results": [priced, see_options]}, request=request)

    result = SerpApi("amazon", SETTINGS).search(Session(httpx.MockTransport(handler)), detection("amazon", "https://www.amazon.com"), "dehumidifier", 20, DESTINATION)
    assert [item["item_ref"]["asin"] for item in result["items"]] == ["B0GL7Q7173", "B09155CW3S"]
    assert result["items"][0]["price"] == {"amount": "209.99", "currency": "USD"}
    assert "price" not in result["items"][1]


def _ebay_handler(routes: dict[str, httpx.Response | Callable[[httpx.Request], httpx.Response]], requests: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/identity/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200}, request=request)
        route = routes[request.url.path.rsplit("/", 1)[-1]]
        return route(request) if callable(route) else route

    return handler


def test_ebay_mints_one_token_and_encodes_contextual_location() -> None:
    requests: list[httpx.Request] = []
    search = httpx.Response(200, json={"itemSummaries": [{"itemId": "v1|1|0", "title": "Valve", "itemWebUrl": "https://www.ebay.com/itm/1?_skw=valve&hash=item1", "price": {"value": "9.99", "currency": "USD"}, "condition": "Used", "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"], "seller": {"username": "shop", "feedbackScore": 12, "feedbackPercentage": "99.5"}, "shippingOptions": [{"shippingCost": {"value": "4.50", "currency": "USD"}}]}]})
    adapter = Ebay({"ebay": {"client_id": "id", "client_secret": "secret"}})
    session = Session(httpx.MockTransport(_ebay_handler({"search": search}, requests)))
    adapter.search(session, detection("ebay", "https://www.ebay.com"), "valve", 20, DESTINATION)
    result = adapter.search(session, detection("ebay", "https://www.ebay.com"), "valve", 20, DESTINATION)
    assert sum(request.url.path == "/identity/v1/oauth2/token" for request in requests) == 1
    assert requests[-1].headers["X-EBAY-C-ENDUSERCTX"] == "contextualLocation=country%3DUS%2Czip%3D94103"
    assert result["items"][0]["product_url"] == "https://www.ebay.com/itm/1"
    assert result["items"][0]["listing"] == {"condition": "Used", "shipping": 4.5, "seller": "shop (12, 99.5%)", "best_offer": True}


def test_ebay_refreshes_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    search = httpx.Response(200, json={"itemSummaries": []})
    adapter = Ebay({"ebay": {"client_id": "id", "client_secret": "secret"}})
    session = Session(httpx.MockTransport(_ebay_handler({"search": search}, requests)))
    adapter.search(session, detection("ebay", "https://www.ebay.com"), "valve", 20, DESTINATION)
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + 7200)
    adapter.search(session, detection("ebay", "https://www.ebay.com"), "valve", 20, DESTINATION)
    assert sum(request.url.path == "/identity/v1/oauth2/token" for request in requests) == 2


def test_ebay_product_url_uses_legacy_id_lookup_and_returns_listing_terms() -> None:
    def item(request: httpx.Request) -> httpx.Response:
        assert request.url.params["legacy_item_id"] == "123456789012"
        return httpx.Response(200, json={
            "itemId": "v1|123456789012|0", "title": "Valve", "itemWebUrl": "https://www.ebay.com/itm/123456789012",
            "price": {"value": "9.99", "currency": "USD"}, "description": "<p>Brass valve</p>", "condition": "New",
            "returnTerms": {"returnsAccepted": True, "returnPeriod": {"value": 30, "unit": "CALENDAR_DAY"}, "returnShippingCostPayer": "BUYER"},
            "localizedAspects": [{"name": "Brand", "value": "Acme"}],
            "estimatedAvailabilities": [{"estimatedAvailabilityStatus": "IN_STOCK", "estimatedSoldQuantity": 3}],
            "shippingOptions": [{"shippingServiceCode": "USPS Ground Advantage", "shippingCost": {"value": "5.00", "currency": "USD"}, "minEstimatedDeliveryDate": "2026-11-05T10:00:00.000Z", "maxEstimatedDeliveryDate": "2026-11-12T10:00:00.000Z"}],
        })

    result = Ebay({"ebay": {"client_id": "id", "client_secret": "secret"}}).product(
        Session(httpx.MockTransport(_ebay_handler({"get_item_by_legacy_id": item}, []))),
        detection("ebay", "https://www.ebay.com"),
        {"url": "https://www.ebay.com/itm/123456789012"},
        DESTINATION,
    )

    assert result["available"] is True
    assert result["description"] == "<p>Brass valve</p>"
    assert result["shipping_options"] == [{"title": "USPS Ground Advantage", "price": 5.0, "delivery": "2026-11-05/2026-11-12"}]
    assert result["listing"] == {"condition": "New", "shipping": 5.0, "sold": 3, "returns": "30 days, buyer pays return shipping", "specifics": {"Brand": "Acme"}}


def test_ebay_multi_variation_url_returns_item_group_variants() -> None:
    legacy = httpx.Response(400, json={"errors": [{"errorId": 11006, "message": "The legacy Id is invalid."}]})
    group = httpx.Response(200, json={"commonDescriptions": [{"description": "Cables"}], "items": [
        {"itemId": f"v1|5|{index}", "title": "Cable", "itemWebUrl": f"https://www.ebay.com/itm/5?var=10{index}", "price": {"value": price, "currency": "USD"}, "localizedAspects": [{"name": "Brand", "value": "Acme"}, {"name": "Length", "value": length}]}
        for index, price, length in ((1, "8.89", "3FT"), (2, "9.99", "6FT"))
    ]})
    result = Ebay({"ebay": {"client_id": "id", "client_secret": "secret"}}).product(
        Session(httpx.MockTransport(_ebay_handler({"get_item_by_legacy_id": legacy, "get_items_by_item_group": group}, []))),
        detection("ebay", "https://www.ebay.com"),
        {"url": "https://www.ebay.com/itm/5"},
        DESTINATION,
    )

    assert [(item["variant"], item["options"], item["item_ref"]["item_id"]) for item in result["items"]] == [
        ("3FT", [{"name": "Length", "value": "3FT"}], "v1|5|1"),
        ("6FT", [{"name": "Length", "value": "6FT"}], "v1|5|2"),
    ]
    assert result["items"][0]["description"] == "Cables"
    assert result["items"][1]["variant_url"] == "https://www.ebay.com/itm/5?var=102"


def test_ebay_api_error_reports_ebay_message() -> None:
    failure = httpx.Response(400, json={"errors": [{"errorId": 11020, "message": "The X-EBAY-C-ENDUSERCTX header value is in an invalid format."}]})
    result = Ebay({"ebay": {"client_id": "id", "client_secret": "secret"}}).product(
        Session(httpx.MockTransport(_ebay_handler({"get_item_by_legacy_id": failure}, []))),
        detection("ebay", "https://www.ebay.com"),
        {"url": "https://www.ebay.com/itm/1"},
        DESTINATION,
    )
    assert result["reason"] == "eBay Browse getItem failed: The X-EBAY-C-ENDUSERCTX header value is in an invalid format."


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

    adapter = ShopifyGlobal()
    result = adapter.search(Session(httpx.MockTransport(handler)), detection("shopify_global", "https://shop.app"), "boot", 10, DESTINATION)
    arguments = bodies[0]["params"]["arguments"]
    assert bodies[0]["params"]["name"] == "search_catalog"
    assert arguments["meta"]["ucp-agent"]["profile"] == "https://cdn.jsdelivr.net/gh/ahalekelly/cross-shop@main/ucp-profile.json"
    assert arguments["catalog"]["filters"]["ships_to"] == {"country": "US", "region": "CA", "postal_code": "94103"}
    assert result["items"][0]["price"] == {"amount": "40.00", "currency": "USD"}
    assert result["items"][0]["item_ref"] == {"platform": "shopify_global", "product_id": "gid://shopify/p/abc", "variant_id": "gid://shopify/ProductVariant/1"}
    assert result["items"][0]["seller_url"] == "https://merchant.test"
    assert result["items"][0]["seller_domain"] == "merchant-api.myshopify.com"
    assert result["items"][0]["variant_url"].endswith("?variant=1")
    assert result["items"][0]["checkout_url"] == "https://merchant.test/cart/1:1"


def test_shopify_global_search_writes_handles_and_shopify_merchants(tmp_path: Path) -> None:
    data = DataStore(tmp_path)
    payload = json.loads((FIXTURES / "marketplace-shopify-global-search.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    result = CrossShop(data, lambda origin: httpx.MockTransport(handler)).search(
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

    result = ShopifyGlobal().product(
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

    result = ShopifyGlobal().search(
        Session(httpx.MockTransport(handler)),
        detection("shopify_global", "https://shop.app"),
        "boot",
        10,
        DESTINATION,
    )

    assert result["status"] == "api_error"
    assert "profile rejected" in result["reason"]


def test_aliexpress_checks_application_response_code() -> None:

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"aliexpress_affiliate_product_query_response": {"resp_result": {"resp_code": 500, "resp_msg": "failed"}}}, request=request)

    result = AliExpress(SETTINGS).search(
        Session(httpx.MockTransport(handler)),
        detection("aliexpress", "https://www.aliexpress.com"),
        "valve",
        10,
        DESTINATION,
    )
    assert result["status"] == "api_error"
    assert "500" in result["reason"]


def test_serpapi_provider_error_redacts_api_key() -> None:

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Invalid key super-secret-key"}, request=request)

    result = SerpApi("google_shopping", {"serpapi": {"api_key": "super-secret-key"}}).search(
        Session(httpx.MockTransport(handler)),
        detection("google_shopping", "https://shopping.google.com"),
        "valve",
        10,
        DESTINATION,
    )
    assert result["status"] == "api_error"
    assert "super-secret-key" not in result["reason"]
    assert "[redacted]" in result["reason"]


