from __future__ import annotations

import json

import httpx
import pytest

from cross_shop.adapters.marketplaces import (
    AMAZON_AOD_PATH,
    AMAZON_USER_AGENT,
    Amazon,
    _amazon_product,
    amazon_asin,
)
from cross_shop.core import DetectedStore, Session, ToolError, validate_ref
from cross_shop.service import CrossShop
from cross_shop.storage import DataStore

RETRIEVED_AT = "2026-08-02T06:00:00Z"
DESTINATION = {
    "country": "US",
    "region": "CA",
    "city": "San Francisco",
    "address1": "747 Howard St",
    "postal_code": "94103",
}
PRODUCT_HTML = """
<div id="aod-container">
  <h5 id="aod-asin-title-text">Example magnetic battery</h5>
  <img id="aod-asin-image-id" src="https://m.media-amazon.com/images/I/example.jpg">
  <i id="aod-asin-reviews-star"><span class="a-icon-alt">4.2 out of 5 stars</span></i>
  <span id="aod-asin-reviews-count-title">3,285 ratings</span>
  <input id="aod-total-offer-count" value="3">
  <div id="aod-pinned-offer">
    <div id="aod-offer-heading">New</div>
    <span class="a-price apex-pricetopay-value">
      <span class="a-price-symbol">$</span>
      <span class="a-price-whole">34.</span>
      <span class="a-price-fraction">16</span>
    </span>
    <div id="aod-offer-shipsFrom">
      <div class="a-fixed-left-grid-col a-col-right"><span>Amazon.com</span></div>
    </div>
    <div id="aod-offer-soldBy">
      <div class="a-fixed-left-grid-col a-col-right"><a>Example Seller</a></div>
    </div>
    <span data-csa-c-delivery-price="FREE"
          data-csa-c-delivery-time="Thursday, August 6"
          data-csa-c-delivery-condition="on orders over $35">
      FREE delivery Thursday, August 6 on orders over $35
    </span>
    <form class="AodAddToCart">
      <input type="hidden" name="anti-csrftoken-a2z" value="secret-token">
    </form>
  </div>
  <div id="aod-offer">
    <div id="aod-offer-heading">Used - Very Good</div>
    <span class="a-price apex-pricetopay-value">
      <span class="a-price-symbol">$</span>
      <span class="a-price-whole">1,234.</span>
      <span class="a-price-fraction">05</span>
    </span>
    <div id="aod-offer-shipsFrom">
      <div class="a-fixed-left-grid-col a-col-right"><span>Marketplace warehouse</span></div>
    </div>
    <div id="aod-offer-soldBy">
      <div class="a-fixed-left-grid-col a-col-right"><span>Second Seller</span></div>
    </div>
    <span data-csa-c-delivery-price="fastest"
          data-csa-c-delivery-time="August 7 - 9"
          data-csa-c-delivery-condition="">Fastest delivery August 7 - 9</span>
    <form class="AodAddToCart"></form>
  </div>
</div>
"""
NO_OFFERS_HTML = """
<div id="aod-container">
  <h5 id="aod-asin-title-text">Unavailable example</h5>
  <img id="aod-asin-image-id" src="https://m.media-amazon.com/images/I/unavailable.jpg">
  <input id="aod-total-offer-count" value="0">
  <div id="aod-pinned-offer">No featured offers available</div>
</div>
"""


def detection() -> DetectedStore:
    return DetectedStore(
        origin="https://www.amazon.com",
        entry_url="https://www.amazon.com/",
        platform="amazon",
        api_origin="https://www.amazon.com",
        evidence=("pseudo",),
    )


def test_normalizes_product_and_offer_panel_without_session_material() -> None:
    result = _amazon_product("B0CZP3CDSZ", PRODUCT_HTML, RETRIEVED_AT)

    assert result["status"] == "found"
    assert result["title"] == "Example magnetic battery"
    assert result["reviews"] == {"status": "rated", "rating": 4.2, "count": 3285}
    offers = result["offers"]
    assert offers["status"] == "offers"
    assert offers["featured"] == {
        "status": "available",
        "offer": {
            "condition": "New",
            "price": {"amount": "34.16", "currency": "USD"},
            "ships_from": "Amazon.com",
            "sold_by": "Example Seller",
            "delivery_promises": [
                {
                    "price_text": "FREE",
                    "time": "Thursday, August 6",
                    "condition": "on orders over $35",
                    "text": "FREE delivery Thursday, August 6 on orders over $35",
                }
            ],
        },
    }
    assert offers["reported_other_offer_count"] == 3
    assert offers["other_offers_complete"] is False
    assert offers["other_offers"][0]["price"] == {
        "amount": "1234.05",
        "currency": "USD",
    }
    assert offers["other_offers"][0]["delivery_promises"][0]["price_text"] == "fastest"
    assert "secret-token" not in json.dumps(result)


def test_models_no_offers_and_no_reviews_explicitly() -> None:
    result = _amazon_product("B0BM4274QM", NO_OFFERS_HTML, RETRIEVED_AT)
    assert result["reviews"] == {"status": "unrated"}
    assert result["offers"] == {"status": "no_offers"}


def test_models_other_offers_without_a_featured_offer() -> None:
    html = PRODUCT_HTML.replace(
        '<div id="aod-offer-heading">New</div>\n'
        '    <span class="a-price apex-pricetopay-value">',
        'No featured offers available\n    <span class="a-price removed-price">',
        1,
    ).replace(
        '<input id="aod-total-offer-count" value="3">',
        '<input id="aod-total-offer-count" value="1">',
    )
    result = _amazon_product("B0CZP3CDSZ", html, RETRIEVED_AT)
    assert result["offers"]["featured"] == {"status": "unavailable"}
    assert result["offers"]["other_offers_complete"] is True


def test_rejects_invalid_offer_states() -> None:
    too_many = PRODUCT_HTML.replace(
        '<input id="aod-total-offer-count" value="3">',
        '<input id="aod-total-offer-count" value="0">',
    )
    with pytest.raises(ToolError, match="more other offers"):
        _amazon_product("B0CZP3CDSZ", too_many, RETRIEVED_AT)

    unknown = NO_OFFERS_HTML.replace("No featured offers available", "")
    with pytest.raises(ToolError, match="no price or unavailable"):
        _amazon_product("B0BM4274QM", unknown, RETRIEVED_AT)


def test_product_uses_one_bootstrap_and_one_read_only_aod_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.content == b""
        assert request.headers["user-agent"] == AMAZON_USER_AGENT
        if request.url.path == "/":
            return httpx.Response(202, request=request)
        assert request.url.path == AMAZON_AOD_PATH
        assert request.url.params["asin"] == "B0CZP3CDSZ"
        assert request.url.params["pc"] == "dp"
        assert request.headers["x-requested-with"] == "XMLHttpRequest"
        return httpx.Response(200, text=PRODUCT_HTML, request=request)

    result = Amazon().product(
        Session(httpx.MockTransport(handler)),
        detection(),
        {"ref": {"platform": "amazon", "asin": "B0CZP3CDSZ"}},
        DESTINATION,
    )

    assert result["title"] == "Example magnetic battery"
    assert result["price"] == {"amount": "34.16", "currency": "USD"}
    assert result["item_ref"] == {"platform": "amazon", "asin": "B0CZP3CDSZ"}
    assert len(requests) == 2


def test_product_does_not_claim_that_aod_404_means_product_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        status = 200 if request.url.path == "/" else 404
        return httpx.Response(status, request=request)

    result = Amazon().product(
        Session(httpx.MockTransport(handler)),
        detection(),
        {"ref": {"platform": "amazon", "asin": "B000IB9QXI"}},
        DESTINATION,
    )

    assert result["status"] == "api_error"
    assert "unavailable" in result["reason"]


def test_product_fails_once_on_a_block_instead_of_retrying() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status = 202 if request.url.path == "/" else 503
        return httpx.Response(status, request=request)

    with pytest.raises(ToolError, match="do not retry or add a bypass"):
        Amazon().product(
            Session(httpx.MockTransport(handler)),
            detection(),
            {"ref": {"platform": "amazon", "asin": "B0CZP3CDSZ"}},
            DESTINATION,
        )
    assert len(requests) == 2


def test_parses_only_us_amazon_asins_and_product_urls() -> None:
    assert amazon_asin("b0czp3cdsz") == "B0CZP3CDSZ"
    assert amazon_asin("https://www.amazon.com/Example-Product/dp/B0CZP3CDSZ?th=1") == "B0CZP3CDSZ"
    assert amazon_asin("https://amazon.com/gp/product/B0CZP3CDSZ/ref=something") == "B0CZP3CDSZ"
    for value in (
        "",
        "not-an-asin",
        "https://www.amazon.co.uk/dp/B0CZP3CDSZ",
        "https://example.com/dp/B0CZP3CDSZ",
        "https://user@amazon.com/dp/B0CZP3CDSZ",
    ):
        with pytest.raises(ToolError):
            amazon_asin(value)


def test_amazon_refs_require_a_valid_asin() -> None:
    assert validate_ref({
        "platform": "amazon",
        "store": "https://www.amazon.com",
        "asin": "B0CZP3CDSZ",
    })["asin"] == "B0CZP3CDSZ"
    with pytest.raises(ToolError, match="invalid asin"):
        validate_ref({
            "platform": "amazon",
            "store": "https://www.amazon.com",
            "asin": "B123",
        })


def test_product_accepts_an_amazon_url_with_query_parameters(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        status = 202 if request.url.path == "/" else 200
        return httpx.Response(status, text=PRODUCT_HTML, request=request)

    result = CrossShop(
        DataStore(tmp_path), lambda origin: httpx.MockTransport(handler)
    ).product(["https://amazon.com/example/dp/B0CZP3CDSZ?th=1"])

    product = result["products"][0]
    assert product["title"] == "Example magnetic battery"
    assert product["price"] == 34.16
    assert product["variants"][0]["ref"] == {
        "platform": "amazon",
        "store": "https://www.amazon.com",
        "asin": "B0CZP3CDSZ",
    }


def test_quote_has_no_anonymous_cart_api() -> None:
    result = Amazon().quote(Session(), detection(), [], DESTINATION)
    assert result == {
        "status": "api_error",
        "platform": "amazon",
        "stage": "quote",
        "reason": "No anonymous Amazon cart API exists",
    }
