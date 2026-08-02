from __future__ import annotations

import json
from pathlib import Path

import httpx

from storefront.adapters import extra
from storefront.core import DetectedStore, Session

FIXTURES = Path(__file__).parent / "fixtures"


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
    result = extra.search(Session(httpx.MockTransport(handler)), detection, "coffee")

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
