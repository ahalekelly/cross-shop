from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from storefront import cli
from storefront.adapters import bigcommerce
from storefront.core import DetectedStore, Session, ToolError, canonical_url, item_ref
from storefront.service import LegacyAdapter, Storefront
from storefront.storage import DataStore


class ProductQuoteAdapter:
    def search(self, session, detection, query, limit, destination):
        del session, detection, query, limit, destination
        return {"kind": "search", "platform": "shopify", "items": []}

    def product(self, session, detection, item, destination):
        del detection, item, destination
        session.request("GET", "https://one.test/live")
        return {
            "title": "Live valve",
            "price": {"amount": "12.50", "currency": "USD"},
            "available": True,
            "product_url": "https://one.test/products/valve",
            "item_ref": item_ref(
                "shopify", {"variant_id": "gid://shopify/ProductVariant/1"}
            ),
        }

    def quote(self, session, detection, lines, destination):
        del session, detection, lines, destination
        return {
            "kind": "quote",
            "platform": "shopify",
            "subtotal": {"amount": "12.50", "currency": "USD"},
            "rates": [],
        }


def registered_data(tmp_path: Path) -> DataStore:
    data = DataStore(tmp_path)
    data.upsert_vendor(
        "https://one.test",
        {
            "platform": "shopify",
            "api_origin": "https://one.test",
            "detected_at": "2026-08-02",
            "evidence": ["registry"],
        },
    )
    return data


def test_config_show_reports_credentials_without_values(tmp_path: Path) -> None:
    data = DataStore(tmp_path)
    data.settings_path.write_text(
        json.dumps(
            {
                "destination": {"country": "US", "postal_code": "94103"},
                "ebay": {"client_id": "client", "client_secret": "secret"},
                "shopify_global": {"profile_url": "https://agent.test/profile"},
                "web_bot_auth": {
                    "private_key_path": "/secret/key.pem",
                    "key_directory_url": "https://agent.test/keys",
                },
            }
        )
    )

    result = cli._config(data, argparse.Namespace(config_command="show"))

    assert result["credentials"] == {
        "ebay": True,
        "shopify_global": True,
        "web_bot_auth": True,
    }
    encoded = json.dumps(result)
    assert "client_secret" not in encoded
    assert "secret" not in encoded
    assert "private_key_path" not in encoded


def test_legacy_product_never_returns_cached_or_guessed_search_data() -> None:
    class Module:
        def search(self, *args):
            raise AssertionError("product resolution must not guess with search")

    adapter = LegacyAdapter(Module())
    detection = DetectedStore(
        origin="https://store.test",
        entry_url="https://store.test/",
        platform="sfcc",
        api_origin="https://store.test",
        evidence=("test",),
    )
    session = Session(httpx.MockTransport(lambda request: None))

    cached = adapter.product(
        session,
        detection,
        {"cached": {"title": "stale"}},
        {"country": "US", "postal_code": "94103"},
    )
    guessed = adapter.product(
        session,
        detection,
        {"url": "https://store.test/products/guessed"},
        {"country": "US", "postal_code": "94103"},
    )

    assert cached["status"] == "api_error"
    assert guessed["status"] == "api_error"


def test_bigcommerce_ref_rejects_a_page_for_another_product() -> None:
    page = (
        Path(__file__).parent / "fixtures" / "platform-bigcommerce-product.html"
    ).read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page, request=request)

    detection = DetectedStore(
        origin="https://store.test",
        entry_url="https://store.test/",
        platform="bigcommerce",
        api_origin="https://store.test",
        evidence=("test",),
    )
    result = bigcommerce.BigCommerce().product(
        Session(httpx.MockTransport(handler)),
        detection,
        {
            "ref": {
                "platform": "bigcommerce",
                "store": "https://store.test",
                "product_id": 999,
                "product_url": "https://store.test/bearing",
            }
        },
        {"country": "US", "postal_code": "94103"},
    )

    assert result["status"] == "api_error"
    assert "identity" in result["reason"]


def test_shopify_url_product_uses_live_ajax_product_endpoint(tmp_path: Path) -> None:
    data = registered_data(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/products/valve.js":
            return httpx.Response(
                200,
                json={
                    "id": 10,
                    "title": "Valve",
                    "handle": "valve",
                    "description": "Live detail",
                    "images": ["https://cdn.test/valve.jpg"],
                    "options": [{"name": "Size", "position": 1}],
                    "variants": [
                        {
                            "id": 11,
                            "title": "Small",
                            "option1": "Small",
                            "available": True,
                            "price": 1250,
                        }
                    ],
                },
                request=request,
            )
        if request.url.path == "/cart.js":
            return httpx.Response(200, json={"currency": "USD"}, request=request)
        raise AssertionError(request.url)

    result = Storefront(data, lambda origin: httpx.MockTransport(handler)).product(
        ["https://one.test/products/valve"]
    )

    assert [request.method for request in requests] == ["GET", "GET"]
    assert result["products"][0]["title"] == "Valve"
    assert result["products"][0]["currency"] == "USD"
    assert result["products"][0]["variants"][0]["ref"]["variant_id"].endswith("/11")


def test_product_and_direct_quote_include_live_identity_currency_and_debug(
    tmp_path: Path,
) -> None:
    data = registered_data(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)

    storefront = Storefront(
        data,
        lambda origin: httpx.MockTransport(handler),
        {"shopify": ProductQuoteAdapter()},
    )
    ref = {
        "platform": "shopify",
        "store": "https://one.test",
        "variant_id": "gid://shopify/ProductVariant/1",
    }

    product = storefront.product([ref], debug=True)["products"][0]
    quote = storefront.quote(
        [{"store": "https://one.test", "lines": [{"item": ref, "quantity": 2}]}],
        debug=True,
    )["stores"][0]

    assert product["currency"] == "USD"
    assert product["detection"]["platform"] == "shopify"
    assert product["evidence"]
    assert quote["detection"]["platform"] == "shopify"
    assert quote["evidence"]
    assert quote["lines"] == [
        {"title": "Live valve", "unit_price": 12.5, "quantity": 2, "line_total": 25.0}
    ]


def test_parallel_store_exception_is_isolated(tmp_path: Path) -> None:
    data = DataStore(tmp_path)
    for origin in ("https://one.test", "https://two.test"):
        data.upsert_vendor(
            origin,
            {
                "platform": "shopify",
                "api_origin": origin,
                "detected_at": "2026-08-02",
                "evidence": ["registry"],
            },
        )

    class Adapter(ProductQuoteAdapter):
        def search(self, session, detection, query, limit, destination):
            if detection.origin == "https://one.test":
                raise ValueError("malformed provider value")
            return super().search(session, detection, query, limit, destination)

    result = Storefront(data, adapters={"shopify": Adapter()}).search(
        [
            {"store": "https://one.test", "query": "bad"},
            {"store": "https://two.test", "query": "good"},
        ]
    )

    assert [store["status"] for store in result["stores"]] == ["api_error", "ok"]
    assert "ValueError: malformed provider value" in result["stores"][0]["reason"]


def test_parallel_results_preserve_input_order(tmp_path: Path) -> None:
    data = DataStore(tmp_path)
    for origin in ("https://slow.test", "https://fast.test"):
        data.upsert_vendor(origin, {"platform": "shopify", "api_origin": origin, "detected_at": "2026-08-02", "evidence": ["registry"]})

    class Adapter(ProductQuoteAdapter):
        def search(self, session, detection, query, limit, destination):
            del session, query, limit, destination
            if detection.origin == "https://slow.test":
                time.sleep(0.02)
            return {"kind": "search", "platform": "shopify", "items": [{"title": detection.origin, "product_url": detection.origin + "/products/item", "item_ref": item_ref("shopify", {"variant_id": "gid://shopify/ProductVariant/1"})}]}

    result = Storefront(data, adapters={"shopify": Adapter()}).search([
        {"store": "https://slow.test", "query": "one"},
        {"store": "https://fast.test", "query": "two"},
    ])

    assert [store["store"] for store in result["stores"]] == ["https://slow.test", "https://fast.test"]


def test_item_discriminator_and_canonical_urls_reject_ambiguous_input(
    tmp_path: Path,
) -> None:
    storefront = Storefront(DataStore(tmp_path))
    with pytest.raises(ToolError, match="product-page URL"):
        storefront._resolve_item("httpfoo")
    with pytest.raises(ToolError, match="query or fragment"):
        canonical_url("https://store.test/products/item?variant=1")


def test_storefront_entry_rejects_redirect_outside_allowlist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "store.test":
            return httpx.Response(
                302, headers={"location": "https://evil.test/"}, request=request
            )
        return httpx.Response(200, request=request)

    session = Session(httpx.MockTransport(handler))
    with pytest.raises(ToolError, match="allowed storefront origins"):
        session.get(
            session.storefront_entry(
                "https://store.test/", ["https://store.test"]
            )
        )


def test_load_run_rejects_expired_run_without_gc(tmp_path: Path) -> None:
    data = DataStore(tmp_path)
    run_id = data.save_run({"stores": []})
    path = data.runs / f"{run_id}.json"
    value = json.loads(path.read_text())
    value["created_at"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    path.write_text(json.dumps(value))

    with pytest.raises(ToolError, match="re-run search"):
        data.load_run(run_id)


def test_cli_exit_codes_follow_api_errors(monkeypatch, tmp_path: Path, capsys) -> None:
    data = DataStore(tmp_path)

    class FakeStorefront:
        def __init__(self, received):
            assert received is data

        def search(self, entries, **kwargs):
            del kwargs
            if entries[0]["query"] == "raise":
                raise ToolError("bad input")
            status = "ok" if entries[0]["query"] == "ok" else "api_error"
            return {"stores": [{"status": status}]}

    monkeypatch.setattr(cli, "DataStore", lambda: data)
    monkeypatch.setattr(cli, "Storefront", FakeStorefront)

    assert cli.main(["search", '[{"store":"https://one.test","query":"ok"}]']) == 0
    assert '"status":"ok"' in capsys.readouterr().out
    assert cli.main(["search", '[{"store":"https://one.test","query":"x"}]']) == 1
    assert '"status":"api_error"' in capsys.readouterr().out
    assert cli.main(["search", '[{"store":"https://one.test","query":"raise"}]']) == 1
    assert "tool_error: bad input" in capsys.readouterr().err
