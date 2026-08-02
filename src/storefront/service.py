from __future__ import annotations

import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from storefront.adapters import bigcommerce, extra, magento, shopify, squarespace, woocommerce
from storefront.adapters.marketplaces import AliExpress, Ebay, SerpApi, ShopifyGlobal
from storefront.core import (
    DetectedStore, MagentoDetectedStore, PositiveDetection, Session, StorefrontBotWall,
    StorefrontDetection, ToolError, UnknownStore, api_error, canonical_ref, canonical_url,
    description, json_object, public_detection, url_origin, validate_ref, wall_system,
)
from storefront.storage import DataStore, HANDLE, validate_destination
from storefront.web_bot_auth import build_signer

SCHEMA_VERSION = "storefront-1"
PSEUDO = {
    "https://shop.app": "shopify_global",
    "https://www.aliexpress.com": "aliexpress",
    "https://shopping.google.com": "google_shopping",
    "https://www.amazon.com": "amazon",
    "https://www.ebay.com": "ebay",
}
STOREFRONT_PLATFORMS = (
    "shopify", "woocommerce", "magento", "bigcommerce", "squarespace",
    "wix", "ecwid", "sfcc",
)
LEGACY_MODULES = {
    "bigcommerce": bigcommerce, "squarespace": squarespace,
    "wix": extra, "ecwid": extra, "sfcc": extra,
}
PASSIVE_DETECTORS = (bigcommerce.detect, squarespace.detect, extra.detect)
ACTIVE_DETECTORS = (
    ("woocommerce", woocommerce.detect),
    ("shopify", shopify.detect),
    ("magento", magento.detect),
)


class BoundaryAdapter:
    def __init__(self, platform: str) -> None:
        self.platform = platform

    def search(self, session: Session, detection: PositiveDetection, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del session, detection, query, limit, destination
        return api_error(self.platform, "search", f"{self.platform} has no supported public catalog adapter")

    def product(self, session: Session, detection: PositiveDetection, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del session, detection, item, destination
        return api_error(self.platform, "product", f"{self.platform} requires a browser workflow")

    def quote(self, session: Session, detection: PositiveDetection, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        del session, detection, lines, destination
        return api_error(self.platform, "quote", f"{self.platform} requires a browser workflow")


class LegacyAdapter:
    """Presents the package-wide session/search/product/quote adapter convention."""

    def __init__(self, module: Any) -> None:
        self.module = module

    def search(self, session: Session, detection: PositiveDetection, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del destination
        result = self.module.search(session, detection, query)
        if isinstance(result.get("items"), list):
            result["items"] = result["items"][:limit]
        return result

    def product(self, session: Session, detection: PositiveDetection, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del destination
        platform = detection.platform
        cached = item.get("cached")
        reference = item.get("ref")
        url = (
            item.get("url")
            or (reference.get("product_url") if isinstance(reference, dict) else None)
            or (cached.get("url") if isinstance(cached, dict) else None)
        )
        if platform == "bigcommerce" and isinstance(url, str):
            response = session.request("GET", url, follow_redirects=True)
            if response.status_code != 200:
                return api_error(platform, "product", "BigCommerce product page failed", response.status_code)
            detail = bigcommerce._product(
                response.text, canonical_url(str(response.url))
            )
            if (
                isinstance(reference, dict)
                and detail["item_ref"]["product_id"] != reference.get("product_id")
            ):
                return api_error(
                    platform,
                    "product",
                    "BigCommerce product page identity no longer matches the ref",
                )
            return detail
        if platform == "squarespace":
            collection = reference.get("collection_url") if isinstance(reference, dict) else url
            if isinstance(collection, str):
                response = session.request("GET", collection, params={"format": "json"}, follow_redirects=True)
                if response.status_code != 200:
                    return api_error(platform, "product", "Squarespace product JSON failed", response.status_code)
                values = squarespace._payload_items(json_object(response, "Squarespace product"), "", detection.origin)
                if isinstance(reference, dict):
                    values = [value for value in values if value.get("sku") == reference.get("sku")]
                if values:
                    return {"kind": "search", "platform": platform, "items": values}
        return api_error(
            platform,
            "product",
            f"{platform} cannot resolve this input to live exact product detail",
        )

    def quote(self, session: Session, detection: PositiveDetection, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        if hasattr(self.module, "quote_many"):
            return self.module.quote_many(session, detection, lines, destination)
        if len(lines) != 1:
            return api_error(detection.platform, "quote", "This storefront adapter could not create a multi-line cart")
        return self.module.quote(session, detection, lines[0]["ref"])


class Storefront:
    def __init__(
        self,
        data: DataStore | None = None,
        transport_factory: Callable[[str], httpx.BaseTransport | None] | None = None,
        adapters: dict[str, Any] | None = None,
    ) -> None:
        self.data = data or DataStore()
        self.transport_factory = transport_factory or (lambda origin: None)
        settings = self.data.settings()
        defaults: dict[str, Any] = {platform: LegacyAdapter(module) for platform, module in LEGACY_MODULES.items()}
        defaults |= {
            "shopify": shopify.Shopify(settings),
            "woocommerce": woocommerce.WooCommerce(),
            "magento": magento.Magento(),
            "aliexpress": AliExpress(), "google_shopping": SerpApi("google_shopping"),
            "amazon": SerpApi("amazon"), "ebay": Ebay(settings),
            "shopify_global": ShopifyGlobal(settings),
            "custom": BoundaryAdapter("custom"), "opencart": BoundaryAdapter("opencart"),
            "zen_cart": BoundaryAdapter("zen_cart"), "unknown": BoundaryAdapter("unknown"),
        }
        self.adapters = defaults | (adapters or {})
        self.web_bot_auth = settings.get("web_bot_auth")

    def search(self, entries: object, *, limit: int = 20, description_chars: int = 300, redetect: bool = False, debug: bool = False) -> dict[str, Any]:
        jobs = _search_entries(entries)
        if not 1 <= limit <= 50:
            raise ToolError("Search limit must be from 1 to 50")
        if description_chars < 0:
            raise ToolError("description-chars must be nonnegative")
        grouped: dict[str, list[dict[str, str]]] = {}
        for job in jobs:
            grouped.setdefault(url_origin(job["store"]), []).append(job)
        results = self._parallel(
            grouped,
            lambda origin, values: self._search_store(
                origin, values, limit, redetect, debug
            ),
            self._search_failure,
        )
        stores = [results[origin] for origin in grouped]
        run_id = self.data.save_run({"kind": "search", "stores": [_cache_store(value) for value in stores]})
        output = _metadata(run_id)
        output["stores"] = [
            _search_output(value, run_id, index, description_chars, debug)
            for index, value in enumerate(stores, 1)
        ]
        return output

    def product(self, inputs: object, *, description_chars: int = 2000, redetect: bool = False, debug: bool = False) -> dict[str, Any]:
        items = _item_array(inputs)
        resolved = [self._resolve_item(value) for value in items]
        grouped: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for index, item in enumerate(resolved):
            grouped.setdefault(item["store"], []).append((index, item))
        stores = self._parallel(
            grouped,
            lambda origin, values: self._product_store(
                origin, values, redetect, debug
            ),
            self._product_failure,
        )
        cache_stores: list[dict[str, Any]] = []
        positions: dict[int, tuple[int, int, dict[str, Any]]] = {}
        products: list[dict[str, Any] | None] = [None] * len(items)
        for store_index, origin in enumerate(grouped, 1):
            values = stores[origin]
            cache_items: list[dict[str, Any]] = []
            identity_positions: dict[str, int] = {}
            for input_index, identity, value in values:
                metadata = {
                    key: value[key]
                    for key in ("detection", "evidence")
                    if key in value
                }
                detail = {
                    key: item
                    for key, item in value.items()
                    if key not in {"detection", "evidence"}
                }
                if detail.get("status") == "api_error":
                    products[input_index] = {**detail, **metadata}
                    continue
                item_index = identity_positions.setdefault(
                    identity, len(cache_items) + 1
                )
                if item_index > len(cache_items):
                    cache_items.append(detail)
                positions[input_index] = (store_index, item_index, metadata)
            cache_stores.append({"store": origin, "items": cache_items})
        run_id = self.data.save_run({"kind": "product", "stores": cache_stores})
        for input_index, (store_index, item_index, metadata) in positions.items():
            cached = cache_stores[store_index - 1]["items"][item_index - 1]
            products[input_index] = {
                **_product_output(
                    cached,
                    f"{run_id}.{store_index}.{item_index}",
                    description_chars,
                ),
                **metadata,
            }
        return {**_metadata(run_id), "products": products}

    def quote(self, quotes: object, *, destination: object | None = None, redetect: bool = False, debug: bool = False) -> dict[str, Any]:
        jobs = _quote_entries(quotes)
        target = validate_destination(destination) if destination is not None else self.data.destination()
        grouped: dict[str, list[tuple[int, dict[str, Any], list[tuple[dict[str, Any], dict[str, Any]]]]]] = {}
        for index, job in enumerate(jobs):
            origin = url_origin(job["store"])
            resolved = []
            for line in job["lines"]:
                item = self._resolve_item(line["item"])
                if item["store"] != origin:
                    raise ToolError(f"Quote item store {item['store']} does not match {origin}")
                resolved.append((line, item))
            grouped.setdefault(origin, []).append((index, job, resolved))
        groups = self._parallel(
            grouped,
            lambda origin, values: self._quote_group(
                origin, values, target, redetect, debug
            ),
            self._quote_failure,
        )
        stores: list[dict[str, Any] | None] = [None] * len(jobs)
        for origin in grouped:
            for index, result in groups[origin]:
                stores[index] = result
        return {**_metadata(), "stores": stores}

    def images_for(self, handle: str, selection: str | None = None) -> list[str]:
        match = HANDLE.fullmatch(handle)
        if match is None or match.group(4) is not None:
            raise ToolError("images requires an item handle such as r7.2.5")
        item = self.data.resolve_handle(handle)
        urls = item.get("image_urls", [])
        if not isinstance(urls, list) or any(not isinstance(value, str) for value in urls):
            raise ToolError(f"Cached item {handle} has invalid image URLs")
        selected = _image_range(selection, len(urls))
        if len(selected) > 10:
            raise ToolError("images downloads at most 10 files per call")
        directory = self.data.images / handle
        directory.mkdir(parents=True, exist_ok=True)
        session = self._session(item["store"])
        paths = []
        try:
            for number in selected:
                response = session.request("GET", urls[number - 1], follow_redirects=True)
                if response.status_code != 200:
                    raise ToolError(f"Image {number} returned HTTP {response.status_code}")
                suffix = _image_suffix(response.headers.get("content-type", ""), urls[number - 1])
                path = (directory / f"{number}{suffix}").resolve()
                path.write_bytes(response.content)
                paths.append(str(path))
        finally:
            session.close()
        return paths

    def _parallel(
        self,
        grouped: dict[str, Any],
        worker: Callable[[str, Any], Any],
        failure: Callable[[str, Any, Exception], Any],
    ) -> dict[str, Any]:
        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {
                pool.submit(worker, key, value): (key, value)
                for key, value in grouped.items()
            }
            for future in as_completed(futures):
                key, value = futures[future]
                try:
                    results[key] = future.result()
                except Exception as error:
                    results[key] = failure(key, value, error)
        return results

    def _search_failure(
        self, origin: str, jobs: list[dict[str, str]], error: Exception
    ) -> dict[str, Any]:
        return {
            "store": origin,
            "input": {"store": jobs[0]["store"]},
            **api_error("unknown", "search", _unexpected(error)),
        }

    def _product_failure(
        self,
        origin: str,
        jobs: list[tuple[int, dict[str, Any]]],
        error: Exception,
    ) -> list[tuple[int, str, dict[str, Any]]]:
        del origin
        failure = api_error("unknown", "product", _unexpected(error))
        return [
            (index, _item_identity(item), failure)
            for index, item in jobs
        ]

    def _quote_failure(
        self,
        origin: str,
        jobs: list[
            tuple[
                int,
                dict[str, Any],
                list[tuple[dict[str, Any], dict[str, Any]]],
            ]
        ],
        error: Exception,
    ) -> list[tuple[int, dict[str, Any]]]:
        return [
            (
                index,
                {
                    "store": origin,
                    "input": job,
                    **api_error("unknown", "quote", _unexpected(error)),
                },
            )
            for index, job, _ in jobs
        ]

    def _session(self, origin: str) -> Session:
        return Session(self.transport_factory(origin))

    def _detect(self, session: Session, store: str, redetect: bool) -> StorefrontDetection:
        origin = url_origin(store)
        pseudo = PSEUDO.get(origin)
        if pseudo:
            return DetectedStore(origin=origin, entry_url=origin + "/", platform=pseudo, api_origin=origin, evidence=("built-in marketplace",))
        registered = self.data.vendor(origin)
        if registered is not None and not redetect:
            return _registry_detection(origin, registered)
        requested = canonical_url(store)
        entry_origins = _entry_origins(origin)
        if registered is not None and isinstance(registered.get("api_origin"), str):
            entry_origins.append(url_origin(registered["api_origin"]))
        homepage = session.get(
            session.storefront_entry(requested, list(dict.fromkeys(entry_origins)))
        )
        entry_url = canonical_url(str(homepage.url))
        detected_origin = url_origin(entry_url)
        detections: list[PositiveDetection] = []
        walls: list[StorefrontBotWall] = []
        for detector in PASSIVE_DETECTORS:
            _collect(detections, walls, detector(homepage, detected_origin, entry_url))
        if walls:
            return walls[0]
        if not detections:
            for platform, detector in ACTIVE_DETECTORS:
                if platform == "shopify":
                    session.signer = build_signer(self.web_bot_auth)
                    value = detector(session, detected_origin, entry_url, homepage)
                elif platform == "magento":
                    if detections or walls:
                        break
                    value = detector(session, detected_origin, entry_url, homepage)
                else:
                    value = detector(session, detected_origin, entry_url)
                _collect(detections, walls, value)
        platforms = {value.platform for value in detections}
        if len(platforms) > 1:
            raise ToolError(f"Conflicting storefront detections: {', '.join(sorted(platforms))}")
        if detections:
            detection = detections[0]
            evidence = tuple(dict.fromkeys(item for value in detections for item in value.evidence))
            if isinstance(detection, MagentoDetectedStore):
                detection = MagentoDetectedStore(origin=detected_origin, entry_url=entry_url, api_origin=detection.api_origin, evidence=evidence, search_source=detection.search_source)
            else:
                detection = DetectedStore(origin=detected_origin, entry_url=entry_url, platform=detection.platform, api_origin=detection.api_origin, evidence=evidence)
            self.data.upsert_vendor(detected_origin, _registry_value(detection))
            if detected_origin != origin:
                self.data.upsert_vendor(origin, _registry_value(detection))
            return detection
        if walls:
            return walls[0]
        system = wall_system(homepage)
        if system:
            return StorefrontBotWall(origin=detected_origin, entry_url=entry_url, evidence=(f"HTTP {homepage.status_code} {system} challenge",), system=system, status=homepage.status_code)
        return UnknownStore(origin=detected_origin, entry_url=entry_url, evidence=(f"No positive platform signal; homepage HTTP {homepage.status_code}",))

    def _search_store(self, origin: str, jobs: list[dict[str, str]], limit: int, redetect: bool, debug: bool) -> dict[str, Any]:
        session = self._session(origin)
        try:
            detection = self._detect(session, jobs[0]["store"], redetect)
            base: dict[str, Any] = {"store": detection.origin, "input": {"store": jobs[0]["store"], **({"query": jobs[0]["query"]} if len(jobs) == 1 else {"queries": [job["query"] for job in jobs]})}, "detection": public_detection(detection, debug)}
            if not isinstance(detection, (DetectedStore, MagentoDetectedStore)):
                return {**base, **api_error("unknown", "detect", detection.kind), **({"evidence": session.evidence} if debug else {})}
            adapter = self.adapters[detection.platform]
            products: dict[str, dict[str, Any]] = {}
            for job in jobs:
                raw = adapter.search(session, detection, job["query"], limit, self.data.destination())
                if raw.get("status") == "api_error":
                    return {**base, **raw, **({"evidence": session.evidence} if debug else {})}
                for item in _raw_items(raw):
                    normalized = _normalize_variant(item, detection.platform, detection.origin)
                    identity = normalized["url"] or canonical_ref(normalized["ref"])
                    if detection.platform == "shopify_global":
                        identity += f"\0{normalized.get('currency', '')}"
                    product = products.get(identity)
                    if product is None:
                        product = _new_product(normalized)
                        products[identity] = product
                    else:
                        _append_variant(product, normalized)
                    if len(jobs) > 1 and job["query"] not in product["queries"]:
                        product["queries"].append(job["query"])
            values = list(products.values())[:limit]
            if detection.platform == "shopify_global":
                self._remember_global_merchants(values)
            currencies = {variant["currency"] for value in values for variant in value["variants"] if variant.get("currency")}
            if len(currencies) > 1 and detection.platform != "shopify_global":
                raise ToolError("A store search returned multiple currencies")
            store_currency = (
                next(iter(currencies))
                if len(currencies) == 1 and detection.platform != "shopify_global"
                else None
            )
            return {
                **base,
                "status": "ok",
                **({"currency": store_currency} if store_currency else {}),
                "items": values,
                **({"evidence": session.evidence} if debug else {}),
            }
        except ToolError as error:
            platform = locals().get("detection").platform if isinstance(locals().get("detection"), (DetectedStore, MagentoDetectedStore)) else "unknown"
            return {"store": origin, "input": {"store": jobs[0]["store"]}, **api_error(platform, "search", str(error)), **({"evidence": session.evidence} if debug else {})}
        finally:
            session.close()

    def _product_store(self, origin: str, jobs: list[tuple[int, dict[str, Any]]], redetect: bool, debug: bool) -> list[tuple[int, str, dict[str, Any]]]:
        session = self._session(origin)
        results: list[tuple[int, str, dict[str, Any]]] = []
        try:
            detection = self._detect(session, origin, redetect)
            if not isinstance(detection, (DetectedStore, MagentoDetectedStore)):
                error = {
                    **api_error("unknown", "detect", detection.kind),
                    "detection": public_detection(detection, debug),
                    **({"evidence": session.evidence} if debug else {}),
                }
                return [(index, _item_identity(item), error) for index, item in jobs]
            adapter = self.adapters[detection.platform]
            fetched: dict[str, dict[str, Any]] = {}
            for index, item in jobs:
                identity = _item_identity(item)
                if identity not in fetched:
                    raw = adapter.product(session, detection, item, self.data.destination())
                    if raw.get("status") == "api_error":
                        fetched[identity] = raw
                    else:
                        variants = [_normalize_variant(value, detection.platform, detection.origin) for value in _raw_items(raw)]
                        if not variants:
                            fetched[identity] = api_error(detection.platform, "product", "Product was not found")
                        else:
                            product = _new_product(variants[0])
                            for variant in variants[1:]:
                                _append_variant(product, variant)
                            fetched[identity] = product
                results.append((index, identity, fetched[identity]))
            metadata = {
                "detection": public_detection(detection, debug),
                **({"evidence": session.evidence} if debug else {}),
            }
            return [
                (index, identity, {**value, **metadata})
                for index, identity, value in results
            ]
        except ToolError as error:
            platform = detection.platform if isinstance(locals().get("detection"), (DetectedStore, MagentoDetectedStore)) else "unknown"
            metadata = {
                **(
                    {"detection": public_detection(detection, debug)}
                    if "detection" in locals()
                    else {}
                ),
                **({"evidence": session.evidence} if debug else {}),
            }
            return [
                (
                    index,
                    _item_identity(item),
                    {**api_error(platform, "product", str(error)), **metadata},
                )
                for index, item in jobs
            ]
        finally:
            session.close()

    def _quote_group(
        self,
        origin: str,
        jobs: list[tuple[int, dict[str, Any], list[tuple[dict[str, Any], dict[str, Any]]]]],
        destination: dict[str, str],
        redetect: bool,
        debug: bool,
    ) -> list[tuple[int, dict[str, Any]]]:
        session = self._session(origin)
        try:
            detection = self._detect(session, jobs[0][1]["store"], redetect)
            results = []
            for index, job, resolved in jobs:
                session.client.cookies.clear()
                results.append(
                    (
                        index,
                        self._quote_entry(
                            session, detection, job, resolved, destination, debug
                        ),
                    )
                )
            return results
        except ToolError as error:
            platform = detection.platform if isinstance(locals().get("detection"), (DetectedStore, MagentoDetectedStore)) else "unknown"
            return [
                (
                    index,
                    {
                        "input": job,
                        "store": origin,
                        **(
                            {"detection": public_detection(detection, debug)}
                            if "detection" in locals()
                            else {}
                        ),
                        **api_error(platform, "quote", str(error)),
                        **({"evidence": session.evidence} if debug else {}),
                    },
                )
                for index, job, _ in jobs
            ]
        finally:
            session.close()

    def _quote_entry(
        self,
        session: Session,
        detection: StorefrontDetection,
        job: dict[str, Any],
        resolved: list[tuple[dict[str, Any], dict[str, Any]]],
        destination: dict[str, str],
        debug: bool,
    ) -> dict[str, Any]:
        base = {
            "input": job,
            "store": url_origin(job["store"]),
            "detection": public_detection(detection, debug),
            **({"evidence": session.evidence} if debug else {}),
        }
        if not isinstance(detection, (DetectedStore, MagentoDetectedStore)):
            return {**base, **api_error("unknown", "detect", detection.kind)}
        adapter = self.adapters[detection.platform]
        try:
            lines = []
            for line, item in resolved:
                cached = item.get("cached")
                selected = (
                    cached.get("selected_variant")
                    if isinstance(cached, dict)
                    else None
                )
                variants = (
                    cached.get("variants", [])
                    if isinstance(cached, dict)
                    else []
                )
                reference = item.get("ref") or (
                    selected.get("ref") if isinstance(selected, dict) else None
                )
                if not isinstance(cached, dict):
                    raw_product = adapter.product(
                        session, detection, item, destination
                    )
                    if raw_product.get("status") == "api_error":
                        return {**base, **raw_product}
                    normalized = [
                        _normalize_variant(
                            value, detection.platform, detection.origin
                        )
                        for value in _raw_items(raw_product)
                    ]
                    if not normalized:
                        return {
                            **base,
                            **api_error(
                                detection.platform,
                                "quote",
                                "Product was not found during live verification",
                            ),
                        }
                    cached = _new_product(normalized[0])
                    for variant in normalized[1:]:
                        _append_variant(cached, variant)
                    variants = cached["variants"]
                    if reference is not None:
                        matches = [
                            variant
                            for variant in variants
                            if canonical_ref(variant["ref"])
                            == canonical_ref(reference)
                        ]
                        if len(matches) != 1:
                            return {
                                **base,
                                **api_error(
                                    detection.platform,
                                    "quote",
                                    "Ref no longer resolves to an exact live variant",
                                ),
                            }
                        selected = matches[0]
                        cached["selected_variant"] = selected
                if reference is None and len(variants) == 1:
                    reference = variants[0].get("ref")
                    cached["selected_variant"] = variants[0]
                if reference is None and len(variants) > 1:
                    names = [value.get("name") for value in variants if isinstance(value, dict)]
                    return {**base, **api_error(detection.platform, "quote", f"Choose a variant handle (<run>.<s>.<i>.<v>): {names}")}
                if reference is None:
                    return {**base, **api_error(detection.platform, "quote", "Product did not resolve to an exact variant")}
                lines.append({"ref": reference, "quantity": line["quantity"], "cached": cached, "url": item.get("url")})
            raw = adapter.quote(session, detection, lines, destination)
            if raw.get("status") == "api_error":
                return {**base, **raw}
            return {**base, "status": "ok", **_quote_output(raw, lines)}
        except ToolError as error:
            return {**base, **api_error(detection.platform, "quote", str(error))}

    def _resolve_item(self, value: object) -> dict[str, Any]:
        if isinstance(value, dict):
            reference = validate_ref(value)
            return {"store": reference["store"], "ref": reference}
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            url = canonical_url(value)
            return {"store": url_origin(url), "url": url}
        if isinstance(value, str) and HANDLE.fullmatch(value):
            cached = self.data.resolve_handle(value)
            selected = cached.get("selected_variant")
            variants = cached.get("variants", [])
            reference = selected.get("ref") if isinstance(selected, dict) else None
            if reference is None and isinstance(variants, list) and len(variants) == 1 and isinstance(variants[0], dict):
                reference = variants[0].get("ref")
            return {"store": cached["store"], "ref": reference, "url": cached.get("url"), "cached": cached}
        raise ToolError("Item must be a product-page URL, handle, or ref object")

    def _remember_global_merchants(self, products: list[dict[str, Any]]) -> None:
        for product in products:
            for merchant in product.get("merchant_stores", []):
                if not isinstance(merchant, dict):
                    raise ToolError("Shopify Global merchant identity must be an object")
                origin = url_origin(merchant.get("url"))
                api_origin = url_origin(merchant.get("api_origin"))
                self.data.upsert_vendor(
                    origin,
                    {
                        "platform": "shopify",
                        "api_origin": api_origin,
                        "detected_at": datetime.now(UTC).date().isoformat(),
                        "evidence": ["global_catalog_result"],
                    },
                )


def _collect(detections: list[PositiveDetection], walls: list[StorefrontBotWall], value: PositiveDetection | StorefrontBotWall | None) -> None:
    if isinstance(value, (DetectedStore, MagentoDetectedStore)):
        detections.append(value)
    elif isinstance(value, StorefrontBotWall):
        walls.append(value)


def _registry_value(detection: PositiveDetection) -> dict[str, Any]:
    value: dict[str, Any] = {"platform": detection.platform, "api_origin": detection.api_origin, "detected_at": datetime.now(UTC).date().isoformat(), "evidence": list(detection.evidence)}
    if isinstance(detection, MagentoDetectedStore):
        value["search_source"] = detection.search_source
    return value


def _registry_detection(origin: str, value: dict[str, Any]) -> PositiveDetection:
    platform, api_origin = value.get("platform"), value.get("api_origin", origin)
    evidence = value.get("evidence", ["vendor registry"])
    registry_platforms = set(STOREFRONT_PLATFORMS) | {"custom", "opencart", "zen_cart", "unknown"}
    if not isinstance(platform, str) or platform not in registry_platforms or not isinstance(api_origin, str) or not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
        raise ToolError(f"Vendor registry entry is not a supported storefront: {origin}")
    if platform == "magento":
        source = value.get("search_source", "graphql")
        if source not in {"graphql", "html"}:
            raise ToolError(f"Magento registry entry has invalid search_source: {origin}")
        return MagentoDetectedStore(origin=origin, entry_url=origin + "/", api_origin=api_origin, evidence=tuple(evidence), search_source=source)
    return DetectedStore(origin=origin, entry_url=origin + "/", platform=platform, api_origin=api_origin, evidence=tuple(evidence))


def _entry_origins(origin: str) -> list[str]:
    parts = urlsplit(origin)
    host = parts.hostname
    if host is None:
        raise AssertionError("Origin has no host")
    alias = host.removeprefix("www.") if host.startswith("www.") else f"www.{host}"
    return list(dict.fromkeys((origin, f"https://{alias}")))


def _raw_items(raw: dict[str, Any]) -> list[dict[str, Any]]:
    if raw.get("kind") == "search":
        values = raw.get("items")
    elif any(key in raw for key in ("item_ref", "product_url", "url", "title", "name")):
        values = [raw]
    else:
        values = []
    if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
        raise ToolError("Adapter items must be an array of objects")
    return values


def _normalize_variant(value: dict[str, Any], platform: str, origin: str) -> dict[str, Any]:
    title = value.get("title", value.get("name"))
    if not isinstance(title, str) or not title:
        raise ToolError(f"{platform} product omitted its title")
    url = value.get("product_url", value.get("url", ""))
    if url and not isinstance(url, str):
        raise ToolError(f"{platform} product URL must be a string")
    reference = value.get("item_ref", value.get("ref"))
    if not isinstance(reference, dict):
        raise ToolError(f"{platform} product omitted its ref")
    if reference.get("platform") != platform:
        raise ToolError(f"{platform} adapter returned a ref for another platform")
    reference = {**reference, "store": origin}
    validate_ref(reference)
    raw_price = value.get("price")
    raw_max_price = value.get("max_price")
    if raw_price is not None and not isinstance(raw_price, dict):
        raise ToolError(f"{platform} product price must be money")
    if raw_max_price is not None and not isinstance(raw_max_price, dict):
        raise ToolError(f"{platform} product max_price must be money")
    image_urls = value.get("image_urls", value.get("images", []))
    if isinstance(image_urls, list) and all(isinstance(item, str) for item in image_urls):
        images = image_urls
    else:
        images = []
    options = value.get("options", [])
    name = value.get("variant")
    if not isinstance(name, str) or not name or name.casefold() == "default title":
        name = " / ".join(str(item.get("label", item.get("value", ""))) for item in options if isinstance(item, dict)) or "Default"
    return {
        "title": title, "description": value.get("description", value.get("short_description", "")),
        "url": _handoff_url(url) if url and platform in {"shopify_global", "sfcc"} else canonical_url(url) if url else "",
        "available": value.get("available"),
        "name": name, "options": options, "ref": reference,
        "price": _amount(raw_price), "max_price": _amount(raw_max_price),
        "currency": raw_price.get("currency") if isinstance(raw_price, dict) else None,
        "image_urls": images,
        **({"lead": True} if value.get("lead") is True else {}),
        **({"shipping_options": value["shipping_options"]} if isinstance(value.get("shipping_options"), list) else {}),
        **({"merchant_stores": value["merchant_stores"]} if isinstance(value.get("merchant_stores"), list) else {}),
        **({"seller_url": value["seller_url"]} if isinstance(value.get("seller_url"), str) else {}),
        **({"seller_domain": value["seller_domain"]} if isinstance(value.get("seller_domain"), str) else {}),
        **({"variant_url": value["variant_url"]} if isinstance(value.get("variant_url"), str) else {}),
        **({"checkout_url": value["checkout_url"]} if isinstance(value.get("checkout_url"), str) else {}),
    }


def _new_product(variant: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": variant["title"], "description": variant["description"],
        "url": variant["url"], "available": variant["available"],
        "image_urls": variant["image_urls"], "variants": [variant], "queries": [],
        **({"lead": True} if variant.get("lead") is True else {}),
        **({"shipping_options": variant["shipping_options"]} if "shipping_options" in variant else {}),
        **({"merchant_stores": variant["merchant_stores"]} if "merchant_stores" in variant else {}),
    }


def _append_variant(product: dict[str, Any], variant: dict[str, Any]) -> None:
    if canonical_ref(variant["ref"]) not in {canonical_ref(value["ref"]) for value in product["variants"]}:
        product["variants"].append(variant)
    product["available"] = bool(product.get("available")) or bool(variant.get("available"))
    product["image_urls"] = list(dict.fromkeys(product["image_urls"] + variant["image_urls"]))
    if "merchant_stores" in variant:
        product["merchant_stores"] = list(
            {
                (value["url"], value["api_origin"]): value
                for value in product.get("merchant_stores", []) + variant["merchant_stores"]
            }.values()
        )


def _amount(value: dict[str, Any] | None) -> int | float | None:
    if value is None:
        return None
    decimal = Decimal(value["amount"])
    return int(decimal) if decimal == decimal.to_integral() else float(decimal)


def _product_price(product: dict[str, Any]) -> int | float | list[int | float] | None:
    prices = sorted({price for value in product["variants"] for price in (value.get("price"), value.get("max_price")) if price is not None})
    if not prices:
        return None
    return prices[0] if len(prices) == 1 else [prices[0], prices[-1]]


def _product_currency(product: dict[str, Any]) -> str | None:
    currencies = {
        value["currency"]
        for value in product["variants"]
        if isinstance(value.get("currency"), str)
    }
    return next(iter(currencies)) if len(currencies) == 1 else None


def _search_output(
    store: dict[str, Any],
    run_id: str,
    store_index: int,
    description_chars: int,
    debug: bool,
) -> dict[str, Any]:
    if store.get("status") == "api_error":
        return _omit({key: value for key, value in store.items() if key != "items"})
    items = []
    multi = isinstance(store.get("input", {}).get("queries"), list)
    global_catalog = store.get("detection", {}).get("platform") == "shopify_global"
    for index, product in enumerate(store["items"], 1):
        variants = product["variants"]
        item = {"i": index, "title": product["title"], "price": _product_price(product), "currency": _product_currency(product) if global_catalog else None, "available": product.get("available"), "url": product.get("url"), "description": description(product.get("description", ""), description_chars), "images": len(product.get("image_urls", [])) or None, "lead": product.get("lead"), "variants": [value["name"] for value in variants] if len(variants) > 1 else None, "queries": product.get("queries") if multi else None}
        items.append(_omit(item))
    return _omit({"s": store_index, "input": store["input"], "store": store["store"], "detection": store["detection"], "status": "ok", "currency": store.get("currency"), "items": items, "evidence": store.get("evidence") if debug else None})


def _product_output(product: dict[str, Any], handle: str, chars: int) -> dict[str, Any]:
    variants = []
    product_price = _product_price(product)
    currency = _product_currency(product)
    for value in product["variants"]:
        variant = {"name": value["name"], "options": value.get("options"), "price": value.get("price") if value.get("price") != product_price else None, "currency": value.get("currency") if currency is None else None, "available": value.get("available"), "ref": value["ref"], "seller_url": value.get("seller_url"), "seller_domain": value.get("seller_domain"), "variant_url": value.get("variant_url"), "checkout_url": value.get("checkout_url")}
        variants.append(_omit(variant))
    return _omit({"handle": handle, "title": product["title"], "description": description(product.get("description", ""), chars), "price": product_price, "currency": currency, "available": product.get("available"), "images": len(product.get("image_urls", [])) or None, "url": product.get("url"), "variants": variants, "shipping_options": product.get("shipping_options")})


def _quote_output(raw: dict[str, Any], lines: list[dict[str, Any]]) -> dict[str, Any]:
    subtotal = raw.get("subtotal")
    currency = subtotal.get("currency") if isinstance(subtotal, dict) else None
    output_lines = []
    for line in lines:
        cached = line.get("cached")
        selected = cached.get("selected_variant") if isinstance(cached, dict) else None
        title = cached.get("title") if isinstance(cached, dict) else None
        unit = selected.get("price") if isinstance(selected, dict) else _product_price(cached) if isinstance(cached, dict) else None
        output_lines.append(_omit({"title": title, "unit_price": unit, "quantity": line["quantity"], "line_total": unit * line["quantity"] if isinstance(unit, (int, float)) else None}))
    rates = raw.get("rates")
    options = rates if isinstance(rates, list) and rates else raw.get("shipping_options", [])
    return _omit({"currency": currency, "lines": output_lines, "subtotal": _amount(subtotal) if isinstance(subtotal, dict) else subtotal, "shipping_options": options})


def _cache_store(store: dict[str, Any]) -> dict[str, Any]:
    return {"store": store["store"], "items": store.get("items", [])}


def _item_identity(item: dict[str, Any]) -> str:
    if isinstance(item.get("ref"), dict):
        return canonical_ref(item["ref"])
    if isinstance(item.get("url"), str):
        return item["url"]
    cached = item.get("cached")
    if isinstance(cached, dict) and isinstance(cached.get("handle"), str):
        return cached["handle"]
    raise ToolError("Item has no identity")


def _handoff_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise ToolError(
            "Handoff URL must be absolute HTTPS without credentials or fragment"
        )
    return value


def _unexpected(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _metadata(run_id: str | None = None) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "observed_at": datetime.now(UTC).isoformat(timespec="seconds"), **({"run": run_id} if run_id else {})}


def _omit(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None and item != [] and item != ""}


def _search_entries(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ToolError("Search entries must be an array of 1 to 100 objects")
    entries = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"store", "query"} or any(not isinstance(entry[key], str) or not entry[key].strip() for key in ("store", "query")):
            raise ToolError("Each search entry must contain exactly nonempty store and query strings")
        entries.append({"store": entry["store"], "query": entry["query"]})
    return entries


def _item_array(value: object) -> list[object]:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise ToolError("Product items must be an array of 1 to 100 entries")
    return value


def _quote_entries(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ToolError("Quotes must be an array of 1 to 20 store entries")
    result = []
    for quote in value:
        if not isinstance(quote, dict) or set(quote) != {"store", "lines"} or not isinstance(quote["store"], str):
            raise ToolError("Each quote must contain exactly store and lines")
        lines = quote["lines"]
        if not isinstance(lines, list) or not 1 <= len(lines) <= 20:
            raise ToolError("Quote lines must be an array of 1 to 20 entries")
        normalized = []
        for line in lines:
            if not isinstance(line, dict) or set(line) != {"item", "quantity"} or type(line["quantity"]) is not int or line["quantity"] < 1:
                raise ToolError("Each quote line requires item and a positive integer quantity")
            normalized.append(dict(line))
        result.append({"store": quote["store"], "lines": normalized})
    return result


def _image_range(value: str | None, count: int) -> list[int]:
    if value is None:
        return list(range(1, count + 1))
    if re.fullmatch(r"[1-9]\d*", value):
        numbers = [int(value)]
    else:
        match = re.fullmatch(r"([1-9]\d*):([1-9]\d*)", value)
        if match is None or int(match.group(1)) > int(match.group(2)):
            raise ToolError("Image range must be N or START:END")
        numbers = list(range(int(match.group(1)), int(match.group(2)) + 1))
    if any(number > count for number in numbers):
        raise ToolError(f"Image range exceeds the cached image count ({count})")
    return numbers


def _image_suffix(content_type: str, url: str) -> str:
    types = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
    if content_type.split(";", 1)[0] in types:
        return types[content_type.split(";", 1)[0]]
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return suffix
    raise ToolError(f"Image response has unsupported content type {content_type!r}")
