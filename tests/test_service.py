from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from storefront.core import DetectedStore, ToolError, canonical_ref, description, item_ref, validate_ref
from storefront.service import Storefront
from storefront.storage import DataStore, DEFAULT_DESTINATION, validate_destination


class FakeAdapter:
    def __init__(self) -> None:
        self.searches: list[str] = []
        self.quotes: list[list[dict[str, object]]] = []
        self.destinations: list[dict[str, str]] = []

    def search(self, session, detection, query, limit, destination):
        del session, detection, limit, destination
        self.searches.append(query)
        if query == "broken":
            return {"status": "api_error", "platform": "shopify", "stage": "search", "reason": "broken"}
        return {
            "kind": "search",
            "platform": "shopify",
            "items": [
                {
                    "name": "Valve",
                    "description": "<p>Stainless &amp; precise</p>",
                    "variant": "Small / Blue",
                    "available": True,
                    "price": {"amount": "10", "currency": "USD"},
                    "product_url": "https://one.test/products/valve",
                    "image_urls": ["https://images.test/one.jpg", "https://images.test/two.png"],
                    "item_ref": item_ref("shopify", {"variant_id": "gid://shopify/ProductVariant/1"}),
                },
                {
                    "name": "Valve",
                    "variant": "Large / Red",
                    "available": False,
                    "price": {"amount": "12.5", "currency": "USD"},
                    "product_url": "https://one.test/products/valve",
                    "item_ref": item_ref("shopify", {"variant_id": "gid://shopify/ProductVariant/2"}),
                },
            ],
        }

    def product(self, session, detection, identity, destination):
        del session, detection, identity, destination
        return self.search(None, None, "detail", 20, {})

    def quote(self, session, detection, lines, destination):
        del session, detection
        self.quotes.append(lines)
        self.destinations.append(destination)
        return {
            "kind": "quote",
            "platform": "shopify",
            "subtotal": {"amount": "25", "currency": "USD"},
            "rates": [{"title": "Ground", "amount": {"amount": "5", "currency": "USD"}}],
        }


@pytest.fixture
def data(tmp_path: Path) -> DataStore:
    store = DataStore(tmp_path)
    store.upsert_vendor(
        "https://one.test",
        {
            "platform": "shopify",
            "api_origin": "https://one.test",
            "detected_at": "2026-08-01",
            "evidence": ["test registry"],
        },
    )
    return store


def tool(data: DataStore, adapter: FakeAdapter, transport_factory=None) -> Storefront:
    return Storefront(data, transport_factory or (lambda origin: httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError(request.url)))), {"shopify": adapter})


def test_search_batches_queries_collapses_variants_and_is_token_lean(data: DataStore) -> None:
    adapter = FakeAdapter()
    result = tool(data, adapter).search(
        [
            {"store": "https://one.test", "query": "valve"},
            {"store": "https://one.test", "query": "steel valve"},
        ],
        description_chars=12,
    )

    assert adapter.searches == ["valve", "steel valve"]
    assert result["run"] == "r1"
    assert result["stores"][0]["s"] == 1
    item = result["stores"][0]["items"][0]
    assert item == {
        "i": 1,
        "title": "Valve",
        "price": [10, 12.5],
        "available": True,
        "url": "https://one.test/products/valve",
        "description": "Stainless &…",
        "images": 2,
        "variants": ["Small / Blue", "Large / Red"],
        "queries": ["valve", "steel valve"],
    }
    encoded = json.dumps(result)
    assert "item_ref" not in encoded
    assert "image_urls" not in encoded
    assert "evidence" not in encoded


def test_product_resolves_handle_and_emits_durable_refs(data: DataStore) -> None:
    adapter = FakeAdapter()
    storefront = tool(data, adapter)
    storefront.search([{"store": "https://one.test", "query": "valve"}])

    result = storefront.product(["r1.1.1"])

    assert result["run"] == "r2"
    product = result["products"][0]
    assert product["handle"] == "r2.1.1"
    assert product["variants"][0]["ref"] == {
        "platform": "shopify",
        "store": "https://one.test",
        "variant_id": "gid://shopify/ProductVariant/1",
    }
    assert "image_urls" not in product


def test_quote_resolves_variant_handle_and_preserves_quantity(data: DataStore) -> None:
    adapter = FakeAdapter()
    storefront = tool(data, adapter)
    storefront.search([{"store": "https://one.test", "query": "valve"}])

    result = storefront.quote(
        [{"store": "https://one.test", "lines": [{"item": "r1.1.1.2", "quantity": 2}]}]
    )

    assert result["stores"][0]["status"] == "ok"
    assert adapter.quotes[0][0]["quantity"] == 2
    assert adapter.quotes[0][0]["ref"]["variant_id"].endswith("/2")


def test_bare_multi_variant_handle_is_an_api_error(data: DataStore) -> None:
    adapter = FakeAdapter()
    storefront = tool(data, adapter)
    storefront.search([{"store": "https://one.test", "query": "valve"}])

    result = storefront.quote(
        [{"store": "https://one.test", "lines": [{"item": "r1.1.1", "quantity": 1}]}]
    )

    assert result["stores"][0]["status"] == "api_error"
    assert "<run>.<s>.<i>.<v>" in result["stores"][0]["reason"]


def test_quote_rejects_line_from_another_store(data: DataStore) -> None:
    ref = {"platform": "shopify", "store": "https://other.test", "variant_id": "gid://shopify/ProductVariant/1"}
    with pytest.raises(ToolError, match="does not match"):
        tool(data, FakeAdapter()).quote(
            [{"store": "https://one.test", "lines": [{"item": ref, "quantity": 1}]}]
        )


def test_debug_restores_detection_evidence(data: DataStore) -> None:
    result = tool(data, FakeAdapter()).search(
        [{"store": "https://one.test", "query": "valve"}], debug=True
    )
    assert result["stores"][0]["detection"]["evidence"] == ["test registry"]


def test_registry_hit_skips_live_detection(data: DataStore) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise AssertionError(request.url)

    tool(data, FakeAdapter(), lambda origin: httpx.MockTransport(handler)).search(
        [{"store": "https://one.test", "query": "valve"}]
    )
    assert requests == 0


def test_redetect_overwrites_a_contradicting_registry_entry(data: DataStore) -> None:
    class BigCommerceAdapter:
        def search(self, session, detection, query, limit, destination):
            del session, detection, query, limit, destination
            return {
                "kind": "search",
                "platform": "bigcommerce",
                "items": [{
                    "name": "Valve",
                    "product_url": "https://one.test/valve",
                    "item_ref": item_ref("bigcommerce", {"product_id": 1, "product_url": "https://one.test/valve"}),
                }],
            }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='<script src="/stencil/app.js"></script>', request=request)

    result = Storefront(
        data,
        lambda origin: httpx.MockTransport(handler),
        {"bigcommerce": BigCommerceAdapter()},
    ).search([{"store": "https://one.test", "query": "valve"}], redetect=True)

    assert result["stores"][0]["detection"]["platform"] == "bigcommerce"
    assert data.vendor("https://one.test")["platform"] == "bigcommerce"


def test_one_store_error_does_not_remove_other_store(tmp_path: Path) -> None:
    data = DataStore(tmp_path)
    for origin in ("https://one.test", "https://two.test"):
        data.upsert_vendor(origin, {"platform": "shopify", "api_origin": origin, "detected_at": "2026-08-01", "evidence": ["test"]})
    result = tool(data, FakeAdapter()).search(
        [
            {"store": "https://one.test", "query": "broken"},
            {"store": "https://two.test", "query": "valve"},
        ]
    )
    assert [store["status"] for store in result["stores"]] == ["api_error", "ok"]


def test_run_counter_is_monotonic_under_concurrent_allocation(tmp_path: Path) -> None:
    stores = [DataStore(tmp_path) for _ in range(10)]
    with ThreadPoolExecutor(max_workers=10) as pool:
        ids = list(pool.map(lambda store: store.save_run({"stores": []}), stores))
    assert sorted(int(value[1:]) for value in ids) == list(range(1, 11))


def test_run_counter_survives_garbage_collection(tmp_path: Path) -> None:
    data = DataStore(tmp_path)
    first = data.save_run({"stores": []})
    path = data.runs / f"{first}.json"
    value = json.loads(path.read_text())
    value["created_at"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    path.write_text(json.dumps(value))

    data.gc_runs()

    assert data.save_run({"stores": []}) == "r2"


def test_garbage_collected_handle_never_resolves(tmp_path: Path) -> None:
    data = DataStore(tmp_path)
    run = data.save_run({"stores": [{"store": "https://one.test", "items": [{}]}]})
    path = data.runs / f"{run}.json"
    value = json.loads(path.read_text())
    value["created_at"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    path.write_text(json.dumps(value))
    data.gc_runs()
    with pytest.raises(ToolError, match="re-run search"):
        data.resolve_handle(f"{run}.1.1")


def test_images_uses_cached_urls_and_range(tmp_path: Path) -> None:
    data = DataStore(tmp_path)
    run = data.save_run({"stores": [{"store": "https://one.test", "items": [{"image_urls": ["https://images.test/1.jpg", "https://images.test/2.png"]}]}]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"image", headers={"content-type": "image/png"}, request=request)

    result = Storefront(data, lambda origin: httpx.MockTransport(handler)).images_for(f"{run}.1.1", "2")
    assert len(result) == 1
    assert result[0].endswith("/2.png")
    assert Path(result[0]).read_bytes() == b"image"


def test_quote_destination_precedence(data: DataStore) -> None:
    adapter = FakeAdapter()
    data.set_destination({"country": "CA", "postal_code": "M5V 3L9"})
    storefront = tool(data, adapter)
    ref = {"platform": "shopify", "store": "https://one.test", "variant_id": "gid://shopify/ProductVariant/1"}
    quote = [{"store": "https://one.test", "lines": [{"item": ref, "quantity": 1}]}]

    storefront.quote(quote)
    storefront.quote(quote, destination={"country": "GB", "postal_code": "SW1A 1AA"})

    assert adapter.destinations == [
        {"country": "CA", "postal_code": "M5V 3L9"},
        {"country": "GB", "postal_code": "SW1A 1AA"},
    ]


def test_refs_are_strict_and_canonically_equal() -> None:
    ref = {"platform": "shopify", "store": "https://one.test", "variant_id": "gid://shopify/ProductVariant/1"}
    assert canonical_ref(ref) == canonical_ref(dict(reversed(list(ref.items()))))
    with pytest.raises(ToolError, match="unknown keys"):
        validate_ref({**ref, "extra": True})
    with pytest.raises(ToolError, match="unknown platform"):
        validate_ref({"platform": "nope", "store": "https://one.test"})


def test_destination_and_description_validation() -> None:
    assert validate_destination({"country": "us", "postal_code": "94103"}) == {"country": "US", "postal_code": "94103"}
    assert description("<p>A &amp; B</p>", 3) == "A &…"
    with pytest.raises(ToolError, match="unknown keys"):
        validate_destination({**DEFAULT_DESTINATION, "phone": "x"})
