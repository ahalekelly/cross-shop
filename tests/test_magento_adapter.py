from __future__ import annotations

import httpx
import pytest

from storefront.adapters import magento
from storefront.core import MagentoDetectedStore, Session, ToolError


def detected() -> MagentoDetectedStore:
    return MagentoDetectedStore(
        origin="https://magento.test",
        entry_url="https://magento.test/",
        api_origin="https://magento.test",
        evidence=("test",),
        search_source="html",
    )


def test_html_search_uses_canonical_route_without_trailing_slash() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.path == "/catalogsearch/result"
        assert request.url.params["q"] == "bearing"
        return httpx.Response(200, text="<html></html>", request=request)

    result = magento.search(
        Session(httpx.MockTransport(handler)), detected(), "bearing"
    )

    assert result["source"] == "html"
    assert paths == ["/catalogsearch/result"]


def test_html_search_accepts_one_same_origin_canonical_redirect() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/catalogsearch/result":
            return httpx.Response(
                302, headers={"Location": "/search/bearing"}, request=request
            )
        return httpx.Response(200, text="<html></html>", request=request)

    magento.search(Session(httpx.MockTransport(handler)), detected(), "bearing")

    assert paths == ["/catalogsearch/result", "/search/bearing"]


def test_html_search_rejects_cross_origin_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "https://other.test/search/bearing"},
            request=request,
        )

    with pytest.raises(ToolError, match="same storefront"):
        magento.search(Session(httpx.MockTransport(handler)), detected(), "bearing")
