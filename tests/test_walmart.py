from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cross_shop.adapters.marketplaces import SerpApi, Walmart, walmart_item_id
from cross_shop.core import DetectedStore, Session, ToolError, validate_ref
from cross_shop.service import CrossShop
from cross_shop.storage import DataStore

FIXTURES = Path(__file__).parent / "fixtures"
DESTINATION = {"country": "US", "region": "CA", "city": "San Francisco", "address1": "747 Howard St", "postal_code": "94103"}
PRODUCT_HTML = (FIXTURES / "marketplace-walmart-product.html").read_text()


def detection() -> DetectedStore:
    return DetectedStore(
        origin="https://www.walmart.com",
        entry_url="https://www.walmart.com/",
        platform="walmart",
        api_origin="https://www.walmart.com",
        evidence=("pseudo",),
    )


def test_search_maps_the_serpapi_walmart_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "secret")
    payload = json.loads((FIXTURES / "marketplace-walmart-search.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["engine"] == "walmart"
        assert request.url.params["query"] == "cordless drill"
        return httpx.Response(200, json=payload, request=request)

    result = Walmart().search(
        Session(httpx.MockTransport(handler)),
        detection(),
        "cordless drill",
        20,
        DESTINATION,
    )

    item = result["items"][0]
    assert result["platform"] == "walmart"
    assert item["price"] == {"amount": "79.88", "currency": "USD"}
    assert item["product_url"] == "https://www.walmart.com/ip/123456789"
    assert item["item_ref"] == {"platform": "walmart", "us_item_id": "123456789"}
    assert item["image_urls"] == ["https://i5.walmartimages.com/asr/one.jpeg"]
    assert item["lead"] is True


def test_search_rejects_a_result_without_an_item_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "secret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"search_metadata": {"status": "Success"}, "organic_results": [{"title": "Drill", "primary_offer": {"offer_price": 9.99, "currency": "USD"}}]}, request=request)

    with pytest.raises(ToolError, match="us_item_id"):
        SerpApi("walmart").search(
            Session(httpx.MockTransport(handler)), detection(), "drill", 5, DESTINATION
        )


def test_product_follows_the_item_redirect_and_parses_next_data() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/ip/123456789":
            return httpx.Response(301, headers={"location": "/ip/Example-cordless-drill/123456789"}, request=request)
        return httpx.Response(200, text=PRODUCT_HTML, request=request)

    result = Walmart().product(
        Session(httpx.MockTransport(handler)),
        detection(),
        {"ref": {"platform": "walmart", "us_item_id": "123456789"}},
        DESTINATION,
    )

    assert [str(request.url) for request in requests] == [
        "https://www.walmart.com/ip/123456789",
        "https://www.walmart.com/ip/Example-cordless-drill/123456789",
    ]
    assert result["title"] == "Example cordless drill"
    assert result["price"] == {"amount": "79.88", "currency": "USD"}
    assert result["product_url"] == "https://www.walmart.com/ip/Example-cordless-drill/123456789"
    assert result["image_urls"] == [
        "https://i5.walmartimages.com/asr/one.jpeg",
        "https://i5.walmartimages.com/asr/two.jpeg",
    ]
    assert result["available"] is True
    assert result["item_ref"] == {"platform": "walmart", "us_item_id": "123456789"}
    assert result["delivery_scope"] == "anonymous_default_location"


def test_product_reports_out_of_stock_without_failing() -> None:
    html = PRODUCT_HTML.replace('"value":"IN_STOCK"', '"value":"OUT_OF_STOCK"')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, request=request)

    result = Walmart().product(
        Session(httpx.MockTransport(handler)),
        detection(),
        {"ref": {"platform": "walmart", "us_item_id": "123456789"}},
        DESTINATION,
    )
    assert result["available"] is False


def test_product_reports_a_wall_instead_of_a_schema_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="<html>Please verify you are human — PerimeterX px-captcha</html>", request=request)

    result = Walmart().product(
        Session(httpx.MockTransport(handler)),
        detection(),
        {"ref": {"platform": "walmart", "us_item_id": "123456789"}},
        DESTINATION,
    )

    assert result["state"] == "bot_wall"
    assert result["reason"] == "perimeterx challenge"
    assert result["http_status"] == 429


def test_product_rejects_an_unrecognized_page_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Redesigned page</body></html>", request=request)

    with pytest.raises(ToolError, match="__NEXT_DATA__"):
        Walmart().product(
            Session(httpx.MockTransport(handler)),
            detection(),
            {"ref": {"platform": "walmart", "us_item_id": "123456789"}},
            DESTINATION,
        )


def test_product_rejects_a_page_for_another_item() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=PRODUCT_HTML, request=request)

    with pytest.raises(ToolError, match="another item"):
        Walmart().product(
            Session(httpx.MockTransport(handler)),
            detection(),
            {"ref": {"platform": "walmart", "us_item_id": "987654321"}},
            DESTINATION,
        )


def test_missing_item_page_is_an_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not found", request=request)

    result = Walmart().product(
        Session(httpx.MockTransport(handler)),
        detection(),
        {"ref": {"platform": "walmart", "us_item_id": "123456789"}},
        DESTINATION,
    )
    assert result["status"] == "api_error"
    assert result["http_status"] == 404


def test_parses_only_walmart_item_ids_and_ip_urls() -> None:
    assert walmart_item_id("123456789") == "123456789"
    assert walmart_item_id("https://www.walmart.com/ip/123456789") == "123456789"
    assert walmart_item_id("https://walmart.com/ip/Example-cordless-drill/123456789/") == "123456789"
    for value in (
        "",
        "not-an-id",
        "https://www.walmart.ca/ip/123456789",
        "https://www.walmart.com/browse/123456789",
        "https://user@www.walmart.com/ip/123456789",
    ):
        with pytest.raises(ToolError):
            walmart_item_id(value)


def test_walmart_refs_require_a_decimal_item_id() -> None:
    assert validate_ref({
        "platform": "walmart",
        "store": "https://www.walmart.com",
        "us_item_id": "123456789",
    })["us_item_id"] == "123456789"
    with pytest.raises(ToolError, match="invalid us_item_id"):
        validate_ref({
            "platform": "walmart",
            "store": "https://www.walmart.com",
            "us_item_id": "12A456789",
        })


def test_product_accepts_a_walmart_url_with_query_parameters(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=PRODUCT_HTML, request=request)

    result = CrossShop(
        DataStore(tmp_path), lambda origin: httpx.MockTransport(handler)
    ).product(["https://walmart.com/ip/Example-cordless-drill/123456789?athbdg=L1600"])

    product = result["products"][0]
    assert product["title"] == "Example cordless drill"
    assert product["price"] == 79.88
    assert product["variants"][0]["ref"] == {
        "platform": "walmart",
        "store": "https://www.walmart.com",
        "us_item_id": "123456789",
    }


def test_quote_declares_the_geo_ip_boundary() -> None:
    result = Walmart().quote(Session(), detection(), [], DESTINATION)
    assert result["status"] == "api_error"
    assert "request IP" in result["reason"]
