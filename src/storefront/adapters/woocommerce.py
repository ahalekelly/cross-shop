from __future__ import annotations

from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx
from storefront.core import (
    DetectedStore,
    Http,
    StorefrontBotWall,
    ToolError,
    WooCommerceQuote,
    WooCommerceSearch,
    WooCommerceShipping,
    api_error,
    bot_wall,
    destination_for,
    gated,
    item_ref,
    json_list,
    json_object,
    minor_money,
    parse_item_ref,
    quote_outcome,
    search_result,
    shipping_option,
    unsupported_configuration,
    wall_system,
)

API_PATH = "/wp-json/wc/store/v1"
def detect(
    http: Http, origin: str, entry_url: str
) -> DetectedStore | StorefrontBotWall | None:
    response = http.get(
        http.woo_products(origin, "__codex_platform_probe__", 1)
    )
    system = wall_system(response)
    if system is not None:
        return StorefrontBotWall(
            origin=origin,
            entry_url=entry_url,
            evidence=(f"HTTP {response.status_code} {system} challenge",),
            system=system,
            status=response.status_code,
        )
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, list) or any(
        not isinstance(product, dict) for product in payload
    ):
        return None
    return DetectedStore(
        origin=origin,
        entry_url=entry_url,
        platform="woocommerce",
        api_origin=origin,
        evidence=("WooCommerce Store API product collection",),
    )


class WooCommerce:
    platform = "woocommerce"

    def search(self, http: Http, detection: DetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del destination
        response = http.get(http.woo_products(detection.origin, query, 20))
        terminal = _terminal_response("search", response)
        if terminal is not None:
            return terminal
        products = json_list(response, "WooCommerce product search")
        return search_result(
            WooCommerceSearch(),
            query,
            [_product_item(product) for product in products][:limit],
        )

    def product(self, http: Http, detection: DetectedStore, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del destination
        reference = item.get("ref")
        product_id = reference.get("product_id") if reference is not None else None
        if isinstance(product_id, int):
            response = http.request(
                "GET", _api_base(detection) + f"/products/{product_id}"
            )
            if response.status_code == 200:
                return _detail(
                    http, detection, json_object(response, "WooCommerce product")
                )
        url = item.get("url")
        if url is not None:
            slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
            response = http.request(
                "GET", _api_base(detection) + "/products", params={"slug": slug}
            )
            if response.status_code == 200:
                values = response.json()
                if isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict):
                    return _detail(http, detection, values[0])
        return api_error(
            self.platform,
            "product",
            "woocommerce cannot resolve this input to live exact product detail",
        )

    def quote(self, http: Http, detection: DetectedStore, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        return _quote(http, detection, lines, destination)


def _detail(
    http: Http, detection: DetectedStore, product: dict[str, Any]
) -> dict[str, Any]:
    """Expands a variable parent into the concrete variations that carry exact refs."""
    if product.get("type") != "variable":
        return _product_item(product)
    product_id = product.get("id")
    response = http.request(
        "GET", _api_base(detection) + f"/products/{product_id}/variations"
    )
    if response.status_code != 200:
        return api_error(
            "woocommerce",
            "product",
            "WooCommerce variations request failed",
            response.status_code,
        )
    values = response.json()
    if not isinstance(values, list):
        raise ToolError("WooCommerce variations response must be an array")
    items = []
    for value in values:
        if not isinstance(value, dict):
            raise ToolError("WooCommerce variation must be an object")
        attributes = value.get("attributes", [])
        merged = {
            **product,
            **value,
            "name": product.get("name"),
            "type": "variation",
            "permalink": product.get("permalink"),
            "description": product.get("description", ""),
            "short_description": product.get("short_description", ""),
            "images": value.get("images", product.get("images", [])),
            "add_to_cart": value.get("add_to_cart", product.get("add_to_cart")),
        }
        item = _product_item(merged)
        item["variant"] = " / ".join(
            str(attribute.get("term", attribute.get("value", "")))
            for attribute in attributes
            if isinstance(attribute, dict)
        )
        item["options"] = attributes
        items.append(item)
    return {"kind": "search", "platform": "woocommerce", "items": items}


def _quote(
    http: Http,
    detection: DetectedStore,
    lines: list[dict[str, Any]],
    destination: dict[str, str],
) -> dict[str, Any]:
    selected_lines = []
    for line in lines:
        selected = parse_item_ref(line["ref"], "woocommerce")
        product_id = selected.get("product_id")
        product_type = selected.get("product_type")
        minimum = selected.get("minimum")
        if type(product_id) is not int or product_id <= 0 or product_type not in {"simple", "variation"}:
            return unsupported_configuration("woocommerce", ["variation"], "Product requires an exact variation")
        if type(minimum) is not int or line["quantity"] < minimum:
            raise ToolError(f"WooCommerce product {product_id} requires quantity {minimum}")
        selected_lines.append((product_id, line["quantity"]))

    base = _api_base(detection)
    cart_response = http.request("GET", f"{base}/cart")
    terminal = _terminal_response("quote", cart_response)
    if terminal is not None:
        return terminal
    _cart_totals(json_object(cart_response, "WooCommerce cart"), "WooCommerce cart")
    token = cart_response.headers.get("Cart-Token")
    if not token:
        raise ToolError("WooCommerce cart response has no Cart-Token")
    headers = {"Cart-Token": token}
    item_keys = []
    clear_cart = False
    result: dict[str, object] | None = None
    quote_data: tuple[dict[str, str], dict[str, Any], list[dict[str, Any]]] | None = None
    try:
        for product_id, quantity in selected_lines:
            response = http.request(
                "POST",
                f"{base}/cart/add-item",
                headers=headers,
                json={"id": product_id, "quantity": quantity},
            )
            terminal = _terminal_response("quote", response)
            if terminal is not None:
                result = terminal
                break
            clear_cart = True
            item_keys.append(
                _selected_item_key(
                    json_object(response, "WooCommerce add item"), product_id
                )
            )
            clear_cart = False
        if result is None:
            update_response = http.request(
                "POST",
                f"{base}/cart/update-customer",
                headers=headers,
                json={
                    "shipping_address": destination_for(
                        "woocommerce", destination
                    )
                },
            )
            terminal = _terminal_response("quote", update_response)
            if terminal is not None:
                result = terminal
            else:
                updated_cart = json_object(
                    update_response, "WooCommerce customer update"
                )
                subtotal, totals = _cart_totals(
                    updated_cart, "WooCommerce customer update"
                )
                quote_data = (
                    subtotal,
                    totals,
                    _shipping_options(updated_cart, subtotal["currency"]),
                )
    finally:
        cleanup_statuses = [
            http.request(
                "DELETE", f"{base}/cart/items/{item_key}", headers=headers
            ).status_code
            for item_key in item_keys
        ]
        if clear_cart or any(status not in {200, 204} for status in cleanup_statuses):
            clear_status = http.request(
                "DELETE", f"{base}/cart/items", headers=headers
            ).status_code
            if clear_status not in {200, 204}:
                raise ToolError(
                    "WooCommerce cart cleanup failed; anonymous cart may contain partial state"
                )
    if result is not None:
        return result
    if quote_data is None:
        raise AssertionError("WooCommerce quote must produce data or a terminal result")
    subtotal, totals, options = quote_data
    cleanup_status = max(cleanup_statuses, default=204)
    return quote_outcome(
        WooCommerceQuote(cart_totals=totals, cleanup_status=cleanup_status),
        options,
        subtotal,
    )


def _api_base(detection: DetectedStore) -> str:
    if detection.platform != "woocommerce":
        raise ToolError(
            "WooCommerce adapter requires a detected WooCommerce API origin"
        )
    return detection.api_origin + API_PATH


def _terminal_response(
    operation: str, response: httpx.Response
) -> dict[str, object] | None:
    system = wall_system(response)
    if system is not None:
        return bot_wall(operation, "woocommerce", response, system)
    if response.status_code in {401, 403}:
        return gated(
            operation,
            "woocommerce",
            str(response.url),
            response,
            "WooCommerce Store API authorization required",
        )
    if response.status_code >= 400:
        raise ToolError(f"WooCommerce {operation} returned HTTP {response.status_code}")
    return None


def _product_item(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError("WooCommerce product must be an object")
    product_id, name, product_type = (
        value.get("id"),
        value.get("name"),
        value.get("type"),
    )
    if (
        type(product_id) is not int
        or product_id <= 0
        or not isinstance(name, str)
        or not isinstance(product_type, str)
    ):
        raise ToolError("WooCommerce product has invalid identity fields")
    prices = value.get("prices")
    if not isinstance(prices, dict):
        raise ToolError("WooCommerce product has no prices object")
    add_to_cart = value.get("add_to_cart")
    if not isinstance(add_to_cart, dict):
        raise ToolError("WooCommerce product has no add_to_cart object")
    minimum = add_to_cart.get("minimum")
    if type(minimum) is not int or minimum <= 0:
        raise ToolError(
            "WooCommerce product minimum quantity must be a positive integer"
        )
    images = value.get("images", [])
    return {
        "name": name,
        "description": value.get("short_description") or value.get("description"),
        "image_urls": [image["src"] for image in images if isinstance(image, dict) and isinstance(image.get("src"), str)],
        "sku": value.get("sku"),
        "type": product_type,
        "available": value.get("is_in_stock"),
        "purchasable": value.get("is_purchasable"),
        "price": minor_money(
            prices.get("price"),
            prices.get("currency_code"),
            prices.get("currency_minor_unit"),
        ),
        "product_url": value.get("permalink"),
        "requires_configuration": product_type not in {"simple", "variation"},
        "item_ref": item_ref(
            "woocommerce",
            {
                "product_id": product_id,
                "product_type": product_type,
                "minimum": minimum,
            },
        ),
    }


def _selected_item_key(cart: dict[str, Any], product_id: int) -> str:
    items = cart.get("items")
    if not isinstance(items, list):
        raise ToolError("WooCommerce add-item response has no items array")
    matches = [
        item
        for item in items
        if isinstance(item, dict)
        and type(item.get("id")) is int
        and item["id"] == product_id
    ]
    if (
        len(matches) != 1
        or not isinstance(matches[0].get("key"), str)
        or not matches[0]["key"]
    ):
        raise ToolError(
            "WooCommerce add-item response does not contain the exact selected product"
        )
    return matches[0]["key"]


def _cart_totals(
    cart: dict[str, Any], context: str
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    totals = cart.get("totals")
    if not isinstance(totals, dict):
        raise ToolError(f"{context} has no totals object")
    currency, digits = totals.get("currency_code"), totals.get("currency_minor_unit")
    normalized: dict[str, dict[str, str]] = {}
    for name in (
        "total_items",
        "total_items_tax",
        "total_discount",
        "total_discount_tax",
        "total_shipping",
        "total_shipping_tax",
        "total_price",
        "total_tax",
    ):
        if totals.get(name) is not None:
            normalized[name] = minor_money(totals[name], currency, digits)
    if "total_items" not in normalized:
        raise ToolError(f"{context} totals has no total_items")
    return normalized["total_items"], normalized


def _shipping_options(
    cart: dict[str, Any], expected_currency: str
) -> list[dict[str, Any]]:
    packages = cart.get("shipping_rates")
    if not isinstance(packages, list):
        raise ToolError("WooCommerce cart has no shipping_rates array")
    options: list[dict[str, Any]] = []
    for package in packages:
        if not isinstance(package, dict) or not isinstance(
            package.get("shipping_rates"), list
        ):
            raise ToolError("WooCommerce shipping package has no shipping_rates array")
        for rate in package["shipping_rates"]:
            options.append(_shipping_option(rate, expected_currency))
    return options


def _shipping_option(rate: Any, expected_currency: str) -> dict[str, Any]:
    if not isinstance(rate, dict):
        raise ToolError("WooCommerce shipping rate must be an object")
    rate_id, name = rate.get("rate_id"), rate.get("name")
    if (
        not isinstance(rate_id, str)
        or not rate_id
        or not isinstance(name, str)
        or not name
    ):
        raise ToolError("WooCommerce shipping rate has invalid identity fields")
    currency, digits = rate.get("currency_code"), rate.get("currency_minor_unit")
    if currency != expected_currency:
        raise ToolError("WooCommerce shipping rate currency differs from cart currency")
    amount = minor_money(rate.get("price"), currency, digits)
    tax_value = minor_money(rate.get("taxes"), currency, digits)
    tax = Decimal(tax_value["amount"])
    disposition = (
        "fallback"
        if rate_id.endswith("_fallback")
        else "pickup"
        if rate_id.startswith("local_pickup")
        else "delivery"
    )
    return shipping_option(
        WooCommerceShipping(
            selected=rate.get("selected"),
            tax={"amount": format(tax, "f"), "currency": currency},
        ),
        rate_id,
        name,
        disposition,
        amount,
    )
