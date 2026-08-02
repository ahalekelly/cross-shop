from __future__ import annotations

import json
import uuid
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from storefront.core import (
    DetectedStore,
    Http,
    SquarespaceQuote,
    SquarespaceSearch,
    SquarespaceShipping,
    ToolError,
    api_error,
    bot_wall,
    canonical_url,
    destination_for,
    gated,
    item_ref,
    json_object,
    money,
    parse_item_ref,
    quote_outcome,
    search_result,
    shipping_option,
    url_origin,
    wall_system,
)

PLATFORM = "squarespace"
MAX_PRODUCT_PAGES = 20
def detect(
    response: httpx.Response, origin: str, entry_url: str
) -> DetectedStore | None:
    evidence: list[str] = []
    if response.headers.get("server", "").casefold() == "squarespace":
        evidence.append("server=Squarespace")
    source = response.text.casefold()
    if "static1.squarespace.com" in source:
        evidence.append("Squarespace static assets")
    if not evidence:
        return None
    return DetectedStore(
        origin=origin,
        entry_url=entry_url,
        platform=PLATFORM,
        api_origin=origin,
        evidence=tuple(evidence),
    )


class SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if "search-result" in classes and values.get("data-url"):
            self.urls.append(values["data-url"] or "")


class Squarespace:
    platform = PLATFORM

    def search(self, http: Http, detection: DetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del destination
        return _search(http, detection, query, limit)

    def product(self, http: Http, detection: DetectedStore, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del destination
        reference = item.get("ref")
        collection = (
            reference["collection_url"] if reference is not None else item.get("url")
        )
        if isinstance(collection, str):
            response = http.request(
                "GET", collection, params={"format": "json"}, follow_redirects=True
            )
            if response.status_code != 200:
                return api_error(
                    PLATFORM,
                    "product",
                    "Squarespace product JSON failed",
                    response.status_code,
                )
            values = _payload_items(
                json_object(response, "Squarespace product"), "", detection.origin
            )
            if reference is not None:
                values = [
                    value for value in values if value["sku"] == reference["sku"]
                ]
            if values:
                return {"kind": "search", "platform": PLATFORM, "items": values}
        return api_error(
            PLATFORM,
            "product",
            f"{PLATFORM} cannot resolve this input to live exact product detail",
        )

    def quote(self, http: Http, detection: DetectedStore, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        return _quote(http, detection, lines, destination)


def _search(http: Http, detection: DetectedStore, query: str, limit: int) -> dict[str, object]:
    if urlsplit(detection.entry_url).path not in {"", "/"}:
        response = http.get(http.squarespace_entry_json(detection.entry_url))
        terminal = _terminal(
            "search", detection.entry_url, response, "public commerce page refused"
        )
        if terminal:
            return terminal
        if response.status_code != 200:
            raise ToolError(
                f"Squarespace commerce page returned HTTP {response.status_code}"
            )
        items = _payload_items(
            json_object(response, "Squarespace commerce page"), query, detection.origin
        )
        return search_result(
            SquarespaceSearch(discovery="explicit_entry_url"), query, items[:limit]
        )

    response = http.get(http.squarespace_search(detection.origin, query))
    terminal = _terminal(
        "search", "/search", response, "public storefront search refused"
    )
    if terminal:
        return terminal
    if response.status_code != 200:
        raise ToolError(f"Squarespace search returned HTTP {response.status_code}")
    parser = SearchParser()
    parser.feed(response.text)
    candidates: list[tuple[str, str]] = []
    candidate_urls: set[str] = set()
    for value in parser.urls:
        try:
            candidate = canonical_url(urljoin(str(response.url), value))
        except ToolError:
            continue
        if url_origin(candidate) == detection.origin and candidate not in candidate_urls:
            candidate_urls.add(candidate)
            candidates.append((candidate, value))

    items: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for candidate, raw_reference in candidates[:MAX_PRODUCT_PAGES]:
        page = http.get(
            http.squarespace_product_json(
                response, raw_reference, detection.origin
            )
        )
        terminal = _terminal("search", candidate, page, "public product data refused")
        if terminal:
            return terminal
        if page.status_code != 200:
            raise ToolError(
                f"Squarespace product data returned HTTP {page.status_code}"
            )
        payload = json_object(page, "Squarespace product data")
        if not _is_product_collection(payload) or not isinstance(
            payload.get("item"), dict
        ):
            continue
        for item in _payload_items(payload, query, detection.origin):
            identity = (item["item_id"], item["sku"])
            if identity not in identities:
                identities.add(identity)
                items.append(item)
    return search_result(
        SquarespaceSearch(discovery="storefront_search"), query, items[:limit]
    )


def _quote(
    http: Http,
    detection: DetectedStore,
    lines: list[dict[str, Any]],
    destination: dict[str, str],
) -> dict[str, object]:
    selected = []
    for line in lines:
        reference = parse_item_ref(line["ref"], PLATFORM)
        if set(reference) != {"collection_url", "item_id", "sku"}:
            raise ToolError("Squarespace item_ref has unexpected fields")
        collection_url = reference["collection_url"]
        item_id = reference["item_id"]
        sku = reference["sku"]
        if not all(
            isinstance(value, str) and value
            for value in (collection_url, item_id, sku)
        ):
            raise ToolError("Squarespace item_ref has invalid product identity")
        if (
            canonical_url(collection_url) != collection_url
            or url_origin(collection_url) != detection.origin
        ):
            raise ToolError(
                "Squarespace item_ref collection URL must be canonical and on the detected storefront"
            )
        selected.append((collection_url, item_id, sku, line["quantity"]))

    http.client.cookies.clear()
    collections: dict[str, dict[str, Any]] = {}
    for collection_url, item_id, sku, _ in selected:
        if collection_url not in collections:
            response = http.request(
                "GET", collection_url, params={"format": "json"}, follow_redirects=True
            )
            terminal = _terminal(
                "quote", collection_url, response, "public collection data refused"
            )
            if terminal:
                return terminal
            if response.status_code != 200:
                raise ToolError(
                    f"Squarespace collection returned HTTP {response.status_code}"
                )
            payload = json_object(response, "Squarespace collection")
            if not _is_product_collection(payload) or not isinstance(
                payload.get("items"), list
            ):
                raise ToolError(
                    "Squarespace item_ref collection is not a product collection"
                )
            collections[collection_url] = payload
        products = [
            value
            for value in collections[collection_url]["items"]
            if isinstance(value, dict) and value.get("id") == item_id
        ]
        if len(products) != 1:
            raise ToolError(
                "Squarespace collection did not contain exactly the selected item"
            )
        variants = products[0].get("variants")
        if not isinstance(variants, list):
            raise ToolError("Squarespace selected item omitted its variants")
        matches = [
            value
            for value in variants
            if isinstance(value, dict) and value.get("sku") == sku
        ]
        if len(matches) != 1:
            raise ToolError(
                "Squarespace selected item did not contain exactly the selected SKU"
            )
        if not _available(matches[0]):
            raise ToolError("Squarespace selected variant is not currently available")

    headers = {"X-CSRF-Token": _cookie(http, "crumb"), "Add-To-Cart-Id": str(uuid.uuid4())}
    cart_token = None
    for _, item_id, sku, quantity in selected:
        response = http.request(
            "POST",
            detection.origin + "/api/commerce/shopping-cart/entries",
            headers=headers,
            json={
                "itemId": item_id,
                "sku": sku,
                "quantity": quantity,
                "additionalFields": None,
            },
        )
        terminal = _terminal(
            "quote",
            "/api/commerce/shopping-cart/entries",
            response,
            "anonymous cart mutation refused",
        )
        if terminal:
            return terminal
        if response.status_code != 200:
            raise ToolError(
                f"Squarespace cart add returned HTTP {response.status_code}"
            )
        added = json_object(response, "Squarespace cart add")
        if added.get("error") or added.get("crumbFail") is True:
            return gated(
                "quote",
                PLATFORM,
                "/api/commerce/shopping-cart/entries",
                response,
                "cart rejected its CSRF token or selected product",
            )
        newly_added = added.get("newlyAdded")
        cart = added.get("shoppingCart")
        entries = cart.get("entries") if isinstance(cart, dict) else None
        if (
            not isinstance(newly_added, dict)
            or not isinstance(cart, dict)
            or not isinstance(entries, list)
        ):
            raise ToolError("Squarespace cart add omitted its selected item or cart")
        if (
            newly_added.get("itemId") != item_id
            or _entry_sku(newly_added) != sku
            or newly_added.get("quantity") != quantity
        ):
            raise ToolError("Squarespace cart add did not return the selected product")
        matches = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("itemId") == item_id
            and _entry_sku(entry) == sku
        ]
        if len(matches) != 1:
            raise ToolError(
                "Squarespace cart did not contain exactly the selected product"
            )
        returned_token = cart.get("cartToken")
        if not isinstance(returned_token, str) or not returned_token:
            raise ToolError("Squarespace cart add omitted its cart token")
        if cart_token is not None and returned_token != cart_token:
            raise ToolError("Squarespace cart token changed across line additions")
        cart_token = returned_token
    if cart_token is None:
        raise ToolError("Squarespace quote requires at least one line")

    shipping_response = http.request(
        "PUT",
        detection.origin + f"/api/3/commerce/cart/{cart_token}/shipping/location",
        headers=headers,
        json=destination_for(PLATFORM, destination),
    )
    terminal = _terminal(
        "quote",
        "/api/3/commerce/cart/[redacted]/shipping/location",
        shipping_response,
        "shipping location update refused",
    )
    if terminal:
        return terminal
    if shipping_response.status_code != 200:
        raise ToolError(
            f"Squarespace shipping location returned HTTP {shipping_response.status_code}"
        )
    result = json_object(shipping_response, "Squarespace shipping location")
    status = result.get("shippingOptionsStatus")
    if status not in {
        "APPLICABLE_SHIPPING_OPTIONS",
        "SHIPPING_NOT_REQUIRED",
        "POSTAL_CODE_NOT_APPLICABLE",
    }:
        raise ToolError(
            f"Squarespace returned unknown shippingOptionsStatus {status!r}"
        )
    subtotal_value = result.get("subtotal")
    if not isinstance(subtotal_value, dict):
        raise ToolError("Squarespace shipping response omitted its subtotal")
    subtotal = money(
        subtotal_value.get("decimalValue"), subtotal_value.get("currencyCode")
    )
    raw_options = result.get("fulfillmentOptions")
    if not isinstance(raw_options, list):
        raise ToolError("Squarespace shipping response omitted fulfillmentOptions")
    options = [_shipping_option(value) for value in raw_options]
    if status == "SHIPPING_NOT_REQUIRED":
        reason = "shipping_not_required"
    elif status == "POSTAL_CODE_NOT_APPLICABLE":
        reason = "postal_code_not_applicable"
    elif not raw_options:
        reason = "empty_rate_list"
    elif not any(value["disposition"] == "delivery" for value in options):
        reason = "no_comparable_delivery_rates"
    else:
        reason = "empty_rate_list"
    return quote_outcome(
        SquarespaceQuote(shipping_options_status=status),
        options,
        subtotal,
        no_quote_reason=reason,
    )


def _payload_items(
    payload: dict[str, Any], query: str, origin: str
) -> list[dict[str, Any]]:
    if not _is_product_collection(payload):
        raise ToolError("Squarespace JSON is not a product collection or product page")
    if isinstance(payload.get("item"), dict):
        products = [payload["item"]]
    elif isinstance(payload.get("items"), list):
        products = payload["items"]
    else:
        raise ToolError("Squarespace commerce JSON omitted item data")
    collection = payload["collection"]
    collection_path = collection.get("fullUrl")
    if not isinstance(collection_path, str) or not collection_path:
        raise ToolError("Squarespace commerce JSON omitted its collection URL")
    collection_url = canonical_url(urljoin(origin + "/", collection_path))
    expected = query.casefold()
    items: list[dict[str, Any]] = []
    for product in products:
        if not isinstance(product, dict):
            raise ToolError("Squarespace product must be an object")
        title = product.get("title")
        product_id = product.get("id")
        variants = product.get("variants")
        product_path = product.get("fullUrl")
        if (
            not isinstance(title, str)
            or not isinstance(product_id, str)
            or not isinstance(variants, list)
            or not isinstance(product_path, str)
        ):
            raise ToolError(
                "Squarespace product omitted its title, ID, URL, or variants"
            )
        product_url = canonical_url(urljoin(origin + "/", product_path))
        for variant in variants:
            if not isinstance(variant, dict) or not isinstance(variant.get("sku"), str):
                raise ToolError("Squarespace variant omitted its SKU")
            on_sale = variant.get("onSale", False)
            if type(on_sale) is not bool:
                raise ToolError("Squarespace variant onSale must be boolean")
            attributes = variant.get("attributes")
            if attributes is not None and not isinstance(attributes, dict):
                raise ToolError("Squarespace variant attributes must be an object")
            searchable = f"{title} {variant['sku']} {json.dumps(attributes or {}, sort_keys=True)}".casefold()
            if expected not in searchable:
                continue
            price = (
                variant.get("salePriceMoney")
                if on_sale
                else variant.get("priceMoney")
            )
            if not isinstance(price, dict):
                raise ToolError("Squarespace variant omitted its price")
            regular = (
                variant.get("priceMoney") if on_sale else None
            )
            assets = product.get("assets", [])
            items.append(
                {
                    "item_ref": item_ref(
                        PLATFORM,
                        {
                            "collection_url": collection_url,
                            "item_id": product_id,
                            "sku": variant["sku"],
                        },
                    ),
                    "item_id": product_id,
                    "title": title,
                    "description": product.get("excerpt") or product.get("body") or "",
                    "image_urls": [asset["assetUrl"] for asset in assets if isinstance(asset, dict) and isinstance(asset.get("assetUrl"), str)],
                    "variant": ", ".join(
                        f"{name}: {value}" for name, value in (attributes or {}).items()
                    )
                    or None,
                    "sku": variant["sku"],
                    "barcode": None,
                    "available": _available(variant),
                    "price": money(price.get("value"), price.get("currency")),
                    "compare_at_price": money(
                        regular.get("value"), regular.get("currency")
                    )
                    if isinstance(regular, dict)
                    else None,
                    "weight": None,
                    "url": product_url,
                    "requires_configuration": False,
                }
            )
    return items


def _is_product_collection(payload: dict[str, Any]) -> bool:
    collection = payload.get("collection")
    website = payload.get("website")
    return (
        isinstance(collection, dict)
        and collection.get("type") == 13
        and isinstance(website, dict)
        and isinstance(website.get("id"), str)
    )


def _available(variant: dict[str, Any]) -> bool:
    unlimited = variant.get("unlimited", False)
    if type(unlimited) is not bool:
        raise ToolError("Squarespace variant unlimited must be boolean")
    quantity = variant.get("qtyInStock", 0)
    if type(quantity) is not int:
        raise ToolError("Squarespace variant qtyInStock must be an integer")
    return unlimited or quantity > 0


def _entry_sku(entry: dict[str, Any]) -> Any:
    variant = entry.get("chosenVariant")
    return variant.get("sku") if isinstance(variant, dict) else None


def _shipping_option(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError("Squarespace fulfillment option must be an object")
    option_id = value.get("key")
    title = value.get("name")
    price = value.get("price")
    if not isinstance(price, dict):
        raise ToolError("Squarespace fulfillment option omitted its price")
    amount = money(price.get("decimalValue"), price.get("currencyCode"))
    is_pickup = value.get("isPickup")
    if not isinstance(is_pickup, bool):
        raise ToolError("Squarespace fulfillment option isPickup must be boolean")
    disposition = "pickup" if is_pickup else "delivery"
    return shipping_option(SquarespaceShipping(), option_id, title, disposition, amount)


def _cookie(http: Http, name: str) -> str:
    matches = {value.value for value in http.client.cookies.jar if value.name == name}
    if len(matches) != 1:
        raise ToolError(f"Squarespace collection must set exactly one {name} cookie")
    return next(iter(matches))


def _terminal(
    operation: str,
    endpoint: str,
    response: Any,
    reason: str,
) -> dict[str, Any] | None:
    system = wall_system(response)
    if system:
        return bot_wall(operation, PLATFORM, response, system)
    if response.status_code in {401, 403}:
        return gated(operation, PLATFORM, endpoint, response, reason)
    return None
