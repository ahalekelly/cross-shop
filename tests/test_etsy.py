from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cross_shop.adapters.marketplaces import Etsy, etsy_listing_id
from cross_shop.core import DetectedStore, Session, ToolError, validate_ref
from cross_shop.service import CrossShop
from cross_shop.storage import DataStore

FIXTURES = Path(__file__).parent / "fixtures"
DESTINATION = {"country": "US", "region": "CA", "city": "San Francisco", "address1": "747 Howard St", "postal_code": "94103"}
CREDENTIALS = {"etsy": {"keystring": "keystring", "shared_secret": "shared"}}
LISTING = json.loads((FIXTURES / "marketplace-etsy-listing.json").read_text())
IMAGES = {"count": 1, "results": [{"listing_id": 1234567890, "listing_image_id": 5, "rank": 0, "url_570xN": "https://i.etsystatic.com/compass_570xN.jpg", "url_fullxfull": "https://i.etsystatic.com/compass_fullxfull.jpg"}]}


def detection() -> DetectedStore:
    return DetectedStore(
        origin="https://www.etsy.com",
        entry_url="https://www.etsy.com/",
        platform="etsy",
        api_origin="https://www.etsy.com",
        evidence=("pseudo",),
    )


def money(amount: int, currency: str = "USD") -> dict[str, object]:
    return {"amount": amount, "divisor": 100, "currency_code": currency}


def test_search_divides_money_and_flags_a_variation_floor_price() -> None:
    plain = {**LISTING, "listing_id": 999, "has_variations": False, "price": money(1550)}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/application/listings/active"
        assert request.headers["x-api-key"] == "keystring:shared"
        assert request.url.params["keywords"] == "brass compass"
        assert request.url.params["limit"] == "10"
        return httpx.Response(200, json={"count": 2, "results": [LISTING, plain]}, request=request)

    result = Etsy(CREDENTIALS).search(
        Session(httpx.MockTransport(handler)), detection(), "brass compass", 10, DESTINATION
    )

    variable, simple = result["items"]
    assert result["platform"] == "etsy"
    assert variable["price"] == {"amount": "42.00", "currency": "USD"}
    assert variable["product_url"] == "https://www.etsy.com/listing/1234567890/handmade-brass-compass"
    assert variable["item_ref"] == {"platform": "etsy", "listing_id": "1234567890"}
    assert variable["available"] is True
    assert variable["variant"] == "Lowest-priced variation"
    assert simple["price"] == {"amount": "15.50", "currency": "USD"}
    assert "variant" not in simple


def test_sold_out_listings_are_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 1, "results": [{**LISTING, "state": "sold_out", "quantity": 0}]}, request=request)

    result = Etsy(CREDENTIALS).search(
        Session(httpx.MockTransport(handler)), detection(), "compass", 10, DESTINATION
    )
    assert result["items"][0]["available"] is False


def test_product_reads_the_listing_and_its_images() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/images"):
            return httpx.Response(200, json=IMAGES, request=request)
        return httpx.Response(200, json=LISTING, request=request)

    result = Etsy(CREDENTIALS).product(
        Session(httpx.MockTransport(handler)),
        detection(),
        {"url": "https://www.etsy.com/listing/1234567890/handmade-brass-compass"},
        DESTINATION,
    )

    assert paths == [
        "/v3/application/listings/1234567890",
        "/v3/application/listings/1234567890/images",
    ]
    assert result["image_urls"] == ["https://i.etsystatic.com/compass_fullxfull.jpg"]
    assert result["description"].startswith("A pocket compass")


def test_quote_batches_buyer_prices_for_the_destination_country() -> None:
    lines = [
        {"ref": {"platform": "etsy", "store": "https://www.etsy.com", "listing_id": "1234567890"}, "quantity": 2},
        {"ref": {"platform": "etsy", "store": "https://www.etsy.com", "listing_id": "77"}, "quantity": 1},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v3/application/listings/batch"
        assert request.url.params["listing_ids"] == "1234567890,77"
        assert request.url.params["includes"] == "BuyerPrice"
        assert request.url.params["buyer_country"] == "US"
        return httpx.Response(200, json={"count": 2, "results": [
            {"listing_id": 1234567890, "buyer_price": {"base_price": money(4200), "shipping_cost": money(650), "is_free_shipping": False}},
            {"listing_id": 77, "buyer_price": {"base_price": money(1000), "shipping_cost": None, "is_free_shipping": True}},
        ]}, request=request)

    result = Etsy(CREDENTIALS).quote(
        Session(httpx.MockTransport(handler)), detection(), lines, DESTINATION
    )

    assert result["kind"] == "quote"
    assert result["subtotal"] == {"amount": "94.00", "currency": "USD"}
    assert result["delivery_scope"] == "destination_country"
    assert result["rates"] == [{
        "option_id": "etsy-buyer-price",
        "title": "Etsy buyer-price shipping to US, one shipment per listing",
        "amount": {"amount": "6.50", "currency": "USD"},
    }]


def test_quote_reports_a_listing_that_does_not_ship_to_the_country() -> None:
    lines = [{"ref": {"platform": "etsy", "store": "https://www.etsy.com", "listing_id": "77"}, "quantity": 1}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 1, "results": [
            {"listing_id": 77, "buyer_price": {"base_price": money(1000), "shipping_cost": None, "is_free_shipping": False}},
        ]}, request=request)

    result = Etsy(CREDENTIALS).quote(
        Session(httpx.MockTransport(handler)), detection(), lines, {**DESTINATION, "country": "AU"}
    )

    assert result["kind"] == "empty"
    assert result["reason"] == "no_etsy_shipping_to_AU"
    assert result["shipping_options"][0]["disposition"] == "unavailable"


def test_quote_requires_every_requested_listing() -> None:
    lines = [
        {"ref": {"platform": "etsy", "store": "https://www.etsy.com", "listing_id": "1"}, "quantity": 1},
        {"ref": {"platform": "etsy", "store": "https://www.etsy.com", "listing_id": "2"}, "quantity": 1},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 1, "results": [
            {"listing_id": 1, "buyer_price": {"base_price": money(500), "shipping_cost": None, "is_free_shipping": True}},
        ]}, request=request)

    with pytest.raises(ToolError, match="every requested listing"):
        Etsy(CREDENTIALS).quote(
            Session(httpx.MockTransport(handler)), detection(), lines, DESTINATION
        )


def test_both_api_key_rejections_name_the_bad_credential() -> None:
    bodies = [
        "Invalid API key: should be in the format 'keystring:shared_secret'.",
        "API key not found or not active, or incorrect shared secret for API key.",
    ]
    for body in bodies:
        def handler(request: httpx.Request, body: str = body) -> httpx.Response:
            return httpx.Response(401, json={"error": body}, request=request)

        with pytest.raises(ToolError, match="settings.etsy keystring and shared_secret"):
            Etsy(CREDENTIALS).search(
                Session(httpx.MockTransport(handler)), detection(), "compass", 10, DESTINATION
            )


def test_other_failures_stay_api_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "Internal Server Error"}, request=request)

    result = Etsy(CREDENTIALS).search(
        Session(httpx.MockTransport(handler)), detection(), "compass", 10, DESTINATION
    )
    assert result["status"] == "api_error"
    assert result["http_status"] == 500


def test_missing_credentials_are_reported_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Etsy must not be called without credentials")

    with pytest.raises(ToolError, match="settings.etsy with keystring and shared_secret"):
        Etsy({}).search(
            Session(httpx.MockTransport(handler)), detection(), "compass", 10, DESTINATION
        )


def test_search_writes_handles_through_the_service(tmp_path: Path) -> None:
    data = DataStore(tmp_path)
    data.settings_path.write_text(json.dumps(CREDENTIALS))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 1, "results": [LISTING]}, request=request)

    result = CrossShop(data, lambda origin: httpx.MockTransport(handler)).search(
        [{"store": "https://www.etsy.com", "query": "brass compass"}]
    )

    store = result["stores"][0]
    assert store["detection"] == {"platform": "etsy"}
    assert store["currency"] == "USD"
    assert store["items"][0]["price"] == 42
    assert data.resolve_handle("r1.1.1")["variants"][0]["ref"] == {
        "platform": "etsy",
        "store": "https://www.etsy.com",
        "listing_id": "1234567890",
    }


def test_parses_only_etsy_listing_ids_and_listing_urls() -> None:
    assert etsy_listing_id("1234567890") == "1234567890"
    assert etsy_listing_id("https://www.etsy.com/listing/1234567890/handmade-brass-compass") == "1234567890"
    assert etsy_listing_id("https://etsy.com/listing/1234567890") == "1234567890"
    for value in (
        "",
        "not-a-listing",
        "https://www.etsy.ca/listing/1234567890",
        "https://www.etsy.com/shop/example",
    ):
        with pytest.raises(ToolError):
            etsy_listing_id(value)


def test_etsy_refs_require_a_decimal_listing_id() -> None:
    assert validate_ref({
        "platform": "etsy",
        "store": "https://www.etsy.com",
        "listing_id": "1234567890",
    })["listing_id"] == "1234567890"
    with pytest.raises(ToolError, match="invalid listing_id"):
        validate_ref({
            "platform": "etsy",
            "store": "https://www.etsy.com",
            "listing_id": "abc",
        })
