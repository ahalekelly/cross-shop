from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from cross_shop.adapters.marketplaces import BESTBUY_FIELDS, BestBuy
from cross_shop.core import DetectedStore, Session, ToolError, validate_ref
from cross_shop.service import CrossShop
from cross_shop.storage import DataStore

DESTINATION = {"country": "US", "region": "CA", "city": "San Francisco", "address1": "747 Howard St", "postal_code": "94103"}
CREDENTIALS = {"bestbuy": {"api_key": "key"}}
PRODUCT = {
    "sku": 6535293,
    "name": "Example 55\" 4K TV",
    "salePrice": 379.99,
    "regularPrice": 499.99,
    "manufacturer": "ExampleBrand",
    "modelNumber": "EX55K",
    "onlineAvailability": True,
    "orderable": "Available",
    "url": "https://www.bestbuy.com/site/example-55-4k-tv/6535293.p?skuId=6535293",
    "image": "https://pisces.bbystatic.com/image/6535293.jpg",
    "thumbnailImage": "https://pisces.bbystatic.com/image/6535293_thumb.jpg",
    "customerReviewAverage": 4.6,
    "customerReviewCount": 1204,
    "shippingCost": 0.0,
    "freeShipping": True,
}


def detection() -> DetectedStore:
    return DetectedStore(
        origin="https://www.bestbuy.com",
        entry_url="https://www.bestbuy.com/",
        platform="best_buy",
        api_origin="https://www.bestbuy.com",
        evidence=("pseudo",),
    )


def test_search_ands_keyword_terms_and_maps_products() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/products((search=4k&search=tv))"
        assert request.url.params["apiKey"] == "key"
        assert request.url.params["format"] == "json"
        assert request.url.params["pageSize"] == "5"
        assert request.url.params["show"] == BESTBUY_FIELDS
        return httpx.Response(200, json={"total": 1, "products": [PRODUCT]}, request=request)

    result = BestBuy(CREDENTIALS).search(
        Session(httpx.MockTransport(handler)), detection(), "4k tv", 5, DESTINATION
    )

    item = result["items"][0]
    assert result["platform"] == "best_buy"
    assert item["title"] == 'Example 55" 4K TV'
    assert item["price"] == {"amount": "379.99", "currency": "USD"}
    assert item["available"] is True
    assert item["item_ref"] == {"platform": "best_buy", "sku": "6535293"}
    assert item["image_urls"] == ["https://pisces.bbystatic.com/image/6535293.jpg"]
    assert item["shipping_options"] == [
        {"scope": "catalog", "free_shipping": True, "shipping_cost": 0.0}
    ]


def test_product_reads_one_sku_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/products/6535293.json"
        assert request.url.params["show"] == BESTBUY_FIELDS
        return httpx.Response(200, json=PRODUCT, request=request)

    result = BestBuy(CREDENTIALS).product(
        Session(httpx.MockTransport(handler)),
        detection(),
        {"ref": {"platform": "best_buy", "sku": "6535293"}},
        DESTINATION,
    )
    assert result["item_ref"] == {"platform": "best_buy", "sku": "6535293"}
    assert result["product_url"].endswith("6535293.p?skuId=6535293")


def test_sold_out_products_are_unavailable_not_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**PRODUCT, "onlineAvailability": False, "orderable": "SoldOut"}, request=request)

    result = BestBuy(CREDENTIALS).product(
        Session(httpx.MockTransport(handler)),
        detection(),
        {"ref": {"platform": "best_buy", "sku": "6535293"}},
        DESTINATION,
    )
    assert result["available"] is False


def test_an_unknown_sku_is_an_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={}, request=request)

    result = BestBuy(CREDENTIALS).product(
        Session(httpx.MockTransport(handler)),
        detection(),
        {"ref": {"platform": "best_buy", "sku": "1"}},
        DESTINATION,
    )
    assert result["status"] == "api_error"
    assert result["http_status"] == 404


def test_an_invalid_key_names_the_bad_credential_and_a_missing_key_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="We were unable to validate your API Key.", request=request)

    with pytest.raises(ToolError, match="settings.bestbuy.api_key"):
        BestBuy(CREDENTIALS).search(
            Session(httpx.MockTransport(handler)), detection(), "tv", 5, DESTINATION
        )

    def unlocated(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="We were unable to locate your API Key.", request=request)

    result = BestBuy(CREDENTIALS).search(
        Session(httpx.MockTransport(unlocated)), detection(), "tv", 5, DESTINATION
    )
    assert result["status"] == "api_error"
    assert result["http_status"] == 403


def test_other_failures_stay_api_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Over rate limit", request=request)

    result = BestBuy(CREDENTIALS).search(
        Session(httpx.MockTransport(handler)), detection(), "tv", 5, DESTINATION
    )
    assert result == {
        "status": "api_error",
        "platform": "best_buy",
        "stage": "search",
        "reason": "Best Buy API request failed",
        "http_status": 429,
    }


def test_missing_credentials_are_reported_before_any_request(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Best Buy must not be called without credentials")

    with pytest.raises(ToolError, match="settings.bestbuy with api_key"):
        BestBuy({}).search(
            Session(httpx.MockTransport(handler)), detection(), "tv", 5, DESTINATION
        )

    result = CrossShop(
        DataStore(tmp_path), lambda origin: httpx.MockTransport(handler)
    ).search([{"store": "https://bestbuy.com", "query": "tv"}])
    store = result["stores"][0]
    assert store["platform"] == "best_buy"
    assert store["status"] == "api_error"
    assert "settings.bestbuy with api_key" in store["reason"]


def test_best_buy_refs_require_a_numeric_sku() -> None:
    assert validate_ref({
        "platform": "best_buy",
        "store": "https://www.bestbuy.com",
        "sku": "6535293",
    })["sku"] == "6535293"
    with pytest.raises(ToolError, match="invalid sku"):
        validate_ref({
            "platform": "best_buy",
            "store": "https://www.bestbuy.com",
            "sku": "6535293X",
        })


def test_quote_declares_the_catalog_shipping_boundary() -> None:
    result = BestBuy(CREDENTIALS).quote(Session(), detection(), [], DESTINATION)
    assert result["status"] == "api_error"
    assert "catalog-level" in result["reason"]
