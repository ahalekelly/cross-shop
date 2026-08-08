from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cross_shop.adapters import extra
from cross_shop.core import DEFAULT_DESTINATION, DetectedStore, Session, StorefrontBotWall, ToolError, parse_item_ref

FIXTURES = Path(__file__).parent / "fixtures"


def detected(platform: str, api_origin: str = "https://shop.example", entry_url: str = "https://shop.example/") -> DetectedStore:
    return DetectedStore(origin="https://shop.example", entry_url=entry_url, platform=platform, api_origin=api_origin, evidence=("test",))


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_ecwid_search_uses_initial_data_api_and_public_product_api() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "shop.example":
            return httpx.Response(
                200,
                content=fixture("platform-extra-ecwid-home.html"),
                request=request,
            )
        if request.url.host == "app.ecwid.com" and request.url.path == "/script.js":
            return httpx.Response(
                200,
                content=fixture("platform-extra-ecwid-script.js"),
                request=request,
            )
        if request.url.host == "us-vir3-storefront-api.ecwid.com":
            assert request.method == "POST"
            assert json.loads(request.content) == {"lang": "en"}
            return httpx.Response(
                200,
                content=fixture("platform-extra-ecwid-initial.json"),
                request=request,
            )
        if request.url.host == "app.ecwid.com" and request.url.path.endswith("/products"):
            assert request.url.params["token"] == "public-test-token"
            assert request.url.params["keyword"] == "coffee"
            return httpx.Response(
                200,
                content=fixture("platform-extra-ecwid-products.json"),
                request=request,
            )
        raise AssertionError(request.url)

    detection = DetectedStore(
        origin="https://shop.example",
        entry_url="https://shop.example/",
        platform="ecwid",
        api_origin="https://app.ecwid.com",
        evidence=("test",),
    )
    result = extra.Ecwid().search(Session(httpx.MockTransport(handler)), detection, "coffee", 20, DEFAULT_DESTINATION)

    assert [request.url.host for request in requests] == [
        "shop.example",
        "app.ecwid.com",
        "us-vir3-storefront-api.ecwid.com",
        "app.ecwid.com",
    ]
    assert result["total"] == 1
    assert result["items"][0]["name"] == "Organic Spoonbender"
    assert result["items"][0]["compare_at_price"] == {
        "amount": "24.0",
        "currency": "USD",
    }


def test_detects_wix_ecwid_and_sfcc_signatures() -> None:
    wix = httpx.Response(200, headers={"server": "Pepyaka"}, content=fixture("platform-extra-wix-home.html"))
    ecwid = httpx.Response(200, content=fixture("platform-extra-ecwid-home.html"))
    sfcc = httpx.Response(200, headers={"x-dw-request-base-id": "base"}, text='<link href="/on/demandware.static/site.css">')

    assert extra.detect(wix, "https://shop.example", "https://shop.example/").platform == "wix"
    assert extra.detect(ecwid, "https://shop.example", "https://shop.example/").platform == "ecwid"
    assert extra.detect(sfcc, "https://shop.example", "https://shop.example/").platform == "sfcc"


def test_conflicting_extra_signatures_fail_loudly() -> None:
    response = httpx.Response(200, headers={"server": "Pepyaka"}, content=fixture("platform-extra-ecwid-home.html"))
    with pytest.raises(ToolError, match="Conflicting storefront signatures"):
        extra.detect(response, "https://shop.example", "https://shop.example/")


def test_challenge_beats_extra_platform_markers() -> None:
    response = httpx.Response(403, headers={"server": "cloudflare", "cf-ray": "ray"}, content=fixture("platform-extra-wix-home.html"))
    result = extra.detect(response, "https://shop.example", "https://shop.example/")
    assert isinstance(result, StorefrontBotWall)


def test_wix_search_bootstraps_token_and_maps_product() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_api/v1/access-tokens":
            return httpx.Response(200, content=fixture("platform-extra-wix-tokens.json"), request=request)
        assert request.url.path == "/_api/catalog-reader-server/api/v1/products/query"
        assert request.headers["authorization"] == "anonymous-wix-test-token"
        payload = json.loads(request.content)
        assert json.loads(payload["query"]["filter"]) == {"name": {"$contains": "wheel"}}
        return httpx.Response(200, content=fixture("platform-extra-wix-products.json"), request=request)

    result = extra.Wix().search(Session(httpx.MockTransport(handler)), detected("wix"), "wheel", 20, DEFAULT_DESTINATION)
    product = result["items"][0]
    assert result["total"] == 1
    assert product["name"] == "Jurassic World - Dino Parade"
    assert product["price"] == {"amount": "169.0", "currency": "EUR"}
    assert parse_item_ref(product["item_ref"], "wix") == {"product_id": product["id"]}


@pytest.mark.parametrize("sku", [False, 7, {}, []])
def test_wix_rejects_non_string_sku(sku: object) -> None:
    product = json.loads(fixture("platform-extra-wix-products.json"))["products"][0]
    product["sku"] = sku
    with pytest.raises(ToolError, match="sku must be a string or null"):
        extra._wix_item(detected("wix"), product)


def test_ecwid_rejects_untrusted_api_base() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "shop.example":
            return httpx.Response(200, content=fixture("platform-extra-ecwid-home.html"), request=request)
        return httpx.Response(200, text='{"apiBaseUrl":"https://attacker.test/storefront/api/v1"}', request=request)

    with pytest.raises(ToolError, match="untrusted storefront API base URL"):
        extra.Ecwid().search(Session(httpx.MockTransport(handler)), detected("ecwid", "https://app.ecwid.com"), "cake", 20, DEFAULT_DESTINATION)


@pytest.mark.parametrize("entry_url, expected", [("https://shop.example/", "/search"), ("https://shop.example/it_IT/", "/it_IT/search"), ("https://shop.example/us/home", "/us/search")])
def test_sfcc_search_is_relative_to_entry_url(entry_url: str, expected: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == expected
        return httpx.Response(200, content=fixture("platform-extra-sfcc-search.html"), request=request)

    result = extra.Sfcc().search(Session(httpx.MockTransport(handler)), detected("sfcc", entry_url=entry_url), "towel", 20, DEFAULT_DESTINATION)
    assert result["endpoint"] == f"https://shop.example{expected}"


def test_sfcc_returns_stable_refs_and_rejects_unrelated_html() -> None:
    def valid(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=fixture("platform-extra-sfcc-search.html"), request=request)

    result = extra.Sfcc().search(Session(httpx.MockTransport(valid)), detected("sfcc"), "towel", 20, DEFAULT_DESTINATION)
    assert [item["id"] for item in result["items"]] == ["12133488", "12133489"]
    assert parse_item_ref(result["items"][0]["item_ref"], "sfcc") == {"pid": "12133488"}

    def invalid(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>unrelated</html>", request=request)

    with pytest.raises(ToolError, match="no SFCC storefront signature"):
        extra.Sfcc().search(Session(httpx.MockTransport(invalid)), detected("sfcc"), "towel", 20, DEFAULT_DESTINATION)


@pytest.mark.parametrize("adapter, api_origin", [(extra.Wix(), "https://shop.example"), (extra.Ecwid(), "https://app.ecwid.com"), (extra.Sfcc(), "https://shop.example")])
def test_extra_quotes_are_explicit_browser_boundaries(adapter: object, api_origin: str) -> None:
    result = adapter.quote(Session(), detected(adapter.platform, api_origin), [], DEFAULT_DESTINATION)
    assert result["status"] == "api_error"
    assert result["browser_required"] is True
