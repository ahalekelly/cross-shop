from __future__ import annotations

import json
import re
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote as url_quote
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

import httpx
from storefront.core import (
    Http,
    MagentoDetectedStore,
    MagentoQuote,
    MagentoSearch,
    MagentoShipping,
    ToolError,
    api_error,
    bot_wall,
    canonical_url,
    destination_for,
    gated,
    item_ref,
    json_list,
    json_object,
    money,
    parse_item_ref,
    quote_outcome,
    search_result,
    shipping_option,
    url_origin,
    wall_system,
)

SEARCH_QUERY = """\
query ProductSearch($search: String!) {
  products(search: $search, pageSize: 10) {
    total_count
    items { __typename name sku stock_status url_key }
  }
}
"""

DETAIL_QUERY = """\
query ProductDetail($sku: String!) {
  storeConfig { product_url_suffix }
  products(search: $sku, pageSize: 20) {
    items {
      __typename name sku stock_status url_key
      description { html }
      short_description { html }
      media_gallery { url }
      price_range {
        minimum_price {
          final_price { value currency }
          regular_price { value currency }
        }
      }
      ... on ConfigurableProduct {
        variants {
          attributes { code label value_index }
          product {
            __typename name sku stock_status
            price_range {
              minimum_price {
                final_price { value currency }
                regular_price { value currency }
              }
            }
          }
        }
      }
    }
  }
}
"""

MAX_SEARCH_RESULTS = 10

DETECT_QUERY = """\
query PlatformProbe {
  storeConfig { base_url }
  products(search: "__codex_platform_probe__", pageSize: 1) { total_count }
}
"""


class DetailSkuNotFound(ToolError):
    pass


class ProductLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        href = values.get("href")
        if href and ({"product-item-link", "product-item-photo"} & set(classes)):
            self.links.append(href)


class ProductPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_ld: list[str] = []
        self.form_sku: str | None = None
        self.itemprop_sku: str | None = None
        self.price: str | None = None
        self.currency: str | None = None
        self.available: bool | None = None
        self.required_fields: list[str] = []
        self.title: str | None = None
        self._json: list[str] | None = None
        self._in_product_form = False
        self._sku_tag: str | None = None
        self._sku_text: list[str] | None = None
        self._title: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if (
            tag == "script"
            and (values.get("type") or "").lower() == "application/ld+json"
        ):
            self._json = []
        if tag == "title":
            self._title = []
        if tag == "form" and values.get("id") == "product_addtocart_form":
            self._in_product_form = True
            self.form_sku = values.get("data-product-sku")
        if self._in_product_form and tag in {"input", "select", "textarea"}:
            name = values.get("name") or ""
            input_type = (values.get("type") or "text").lower()
            required = "required" in values or name.startswith(
                ("super_attribute[", "options[")
            )
            if name and input_type != "hidden" and required:
                self.required_fields.append(name)
        itemprop = (values.get("itemprop") or "").lower()
        content = values.get("content")
        if itemprop == "sku" and content:
            self.itemprop_sku = content
        if itemprop == "sku" and content is None:
            self._sku_tag = tag
            self._sku_text = []
        if itemprop == "price" and content:
            self.price = content
        if itemprop == "pricecurrency" and content:
            self.currency = content
        availability = (values.get("property") or "").lower()
        if availability == "product:availability" and content:
            self.available = "in stock" in content.lower() or content.lower().endswith(
                "instock"
            )
        classes = set((values.get("class") or "").lower().split())
        if "stock" in classes and "available" in classes:
            self.available = True
        if "stock" in classes and "unavailable" in classes:
            self.available = False

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._json is not None:
            self.json_ld.append("".join(self._json))
            self._json = None
        if tag == "title" and self._title is not None:
            self.title = " ".join("".join(self._title).split()) or None
            self._title = None
        if tag == self._sku_tag and self._sku_text is not None:
            self.itemprop_sku = " ".join("".join(self._sku_text).split()) or None
            self._sku_tag = None
            self._sku_text = None
        if tag == "form" and self._in_product_form:
            self._in_product_form = False

    def handle_data(self, data: str) -> None:
        if self._json is not None:
            self._json.append(data)
        if self._title is not None:
            self._title.append(data)
        if self._sku_text is not None:
            self._sku_text.append(data)


def detect(
    http: Http,
    origin: str,
    entry_url: str,
    homepage: httpx.Response,
) -> MagentoDetectedStore | None:
    markers = _homepage_markers(homepage)
    graphql = http.request(
        "POST",
        origin + "/graphql",
        headers={"Content-Type": "application/json"},
        json={"query": DETECT_QUERY, "variables": {}},
    )
    if (
        300 <= graphql.status_code < 400
        or graphql.status_code == 429
        or graphql.status_code >= 500
    ):
        raise ToolError(
            f"Magento capability query returned transient HTTP {graphql.status_code}"
        )
    if graphql.status_code in {400, 401, 403, 404, 405, 422}:
        return _html_detection(origin, entry_url, markers)
    if graphql.status_code != 200:
        if markers:
            raise ToolError(
                f"Magento capability query returned unexpected HTTP {graphql.status_code}"
            )
        return None
    try:
        value = graphql.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _html_detection(origin, entry_url, markers)
    if not isinstance(value, dict):
        if markers:
            raise ToolError(
                "Magento capability query returned a contradictory JSON shape"
            )
        return None
    data = value.get("data")
    config = data.get("storeConfig") if isinstance(data, dict) else None
    products = data.get("products") if isinstance(data, dict) else None
    config_proof = (
        isinstance(config, dict)
        and isinstance(config.get("base_url"), str)
        and bool(config["base_url"])
    )
    usable_products = (
        isinstance(products, dict)
        and type(products.get("total_count")) is int
        and products["total_count"] >= 0
    )
    errors = value.get("errors")
    if config_proof and usable_products:
        return MagentoDetectedStore(
            origin=origin,
            entry_url=entry_url,
            api_origin=origin,
            evidence=("magento_graphql_product_search",),
            search_source="graphql",
        )
    if isinstance(errors, list) and errors:
        html = _html_detection(origin, entry_url, markers)
        if html is not None:
            return html
        if config_proof:
            raise ToolError(
                "Magento capability query returned contradictory errors without independent proof"
            )
        return None
    if markers or config_proof:
        raise ToolError(
            "Magento capability query returned a contradictory proven-Magento shape"
        )
    return None


def _html_detection(
    origin: str, entry_url: str, markers: list[str]
) -> MagentoDetectedStore | None:
    if not markers:
        return None
    return MagentoDetectedStore(
        origin=origin,
        entry_url=entry_url,
        api_origin=origin,
        evidence=tuple(markers),
        search_source="html",
    )


def _homepage_markers(homepage: httpx.Response) -> list[str]:
    source = homepage.text[:2_000_000].lower()
    markers = []
    if re.search(r"/static/(?:version\d+/)?frontend/", source):
        markers.append("magento_static_asset")
    if "x-magento-init" in source:
        markers.append("magento_x_magento_init")
    if "mage-cache-" in source:
        markers.append("magento_mage_cache")
    if "private_content_version" in source:
        markers.append("magento_private_content_version")
    if any(name.lower().startswith("x-magento-") for name in homepage.headers):
        markers.append("magento_response_header")
    return markers


class Magento:
    platform = "magento"

    def search(self, http: Http, detection: MagentoDetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del destination
        if detection.search_source == "graphql":
            return _graphql_search(http, detection, query, limit)
        if detection.search_source == "html":
            return _html_search(http, detection, query, limit)
        raise AssertionError(detection.search_source)

    def product(self, http: Http, detection: MagentoDetectedStore, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del destination
        reference = item.get("ref")
        sku = reference.get("sku") if reference is not None else None
        if isinstance(sku, str) and detection.search_source == "graphql":
            envelope, errors = _graphql_detail(http, detection.api_origin, sku)
            if envelope is None:
                return api_error(self.platform, "product", str(errors))
            values, _ = _detail_items(envelope, sku, detection.entry_url)
            return {"kind": "search", "platform": self.platform, "items": values}
        return api_error(
            self.platform,
            "product",
            "magento cannot resolve this input to live exact product detail",
        )

    def quote(self, http: Http, detection: MagentoDetectedStore, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        return _quote(http, detection, lines, destination)


def _graphql_search(
    http: Http, detection: MagentoDetectedStore, query: str, limit: int
) -> dict[str, object]:
    origin = detection.api_origin
    response = http.request(
        "POST",
        origin + "/graphql",
        headers={"Content-Type": "application/json"},
        json={"query": SEARCH_QUERY, "variables": {"search": query}},
    )
    denied = _denied(
        "search", "/graphql", response, "public Magento GraphQL search refused"
    )
    if denied:
        return denied
    if response.status_code != 200:
        raise ToolError(f"Magento GraphQL search returned HTTP {response.status_code}")

    envelope = json_object(response, "Magento GraphQL search")
    errors = _graphql_errors(envelope, "search")
    products = _products(envelope)
    if products is None:
        if errors:
            raise ToolError(
                "Magento GraphQL search returned errors without product data"
            )
        raise ToolError("Magento GraphQL search has no products data")
    candidates = products.get("items")
    if not isinstance(candidates, list):
        raise ToolError("Magento GraphQL search items must be an array")
    if not candidates:
        return search_result(
            MagentoSearch(
                source="graphql",
                api_errors=tuple(errors),
                configurable_products_omitted=0,
            ),
            query,
            [],
        )

    items: list[dict[str, Any]] = []
    omitted = 0
    for candidate in candidates[:MAX_SEARCH_RESULTS]:
        if (
            not isinstance(candidate, dict)
            or not isinstance(candidate.get("sku"), str)
            or not candidate["sku"]
        ):
            raise ToolError("Magento GraphQL search candidate has no SKU")
        detail, detail_errors = _graphql_detail(http, origin, candidate["sku"])
        errors.extend(detail_errors)
        if detail is None:
            continue
        try:
            concrete, unsupported = _detail_items(
                detail, candidate["sku"], detection.entry_url
            )
        except DetailSkuNotFound as error:
            errors.append(
                {
                    "stage": "detail",
                    "sku": candidate["sku"],
                    "message": str(error),
                }
            )
            continue
        items.extend(concrete)
        omitted += unsupported
    if not items and errors:
        raise ToolError("Magento GraphQL detail failed for every search candidate")
    return search_result(
        MagentoSearch(
            source="graphql",
            api_errors=tuple(errors),
            configurable_products_omitted=omitted,
        ),
        query,
        _unique_skus(items)[:limit],
    )


def _quote(
    http: Http,
    detection: MagentoDetectedStore,
    lines: list[dict[str, Any]],
    destination: dict[str, str],
) -> dict[str, object]:
    origin = detection.api_origin
    selected = []
    for line in lines:
        reference = parse_item_ref(line["ref"], "magento")
        sku = reference.get("sku")
        if set(reference) != {"sku"} or not isinstance(sku, str) or not sku:
            raise ToolError(
                "Magento item_ref must contain exactly one nonempty simple SKU"
            )
        if any(character in sku for character in "\r\n"):
            raise ToolError("Magento item_ref SKU cannot contain a newline")
        selected.append((sku, line["quantity"]))

    create = http.request(
        "POST",
        origin + "/rest/V1/guest-carts",
        headers={"Content-Type": "application/json"},
        content=b"{}",
    )
    denied = _denied("quote", "/rest/V1/guest-carts", create, "guest cart API refused")
    if denied:
        return denied
    if create.status_code not in {200, 201}:
        raise ToolError(f"Magento guest cart returned HTTP {create.status_code}")
    try:
        token = create.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ToolError("Magento guest cart did not return JSON") from error
    if not isinstance(token, str) or not 16 <= len(token) <= 128:
        raise ToolError("Magento guest cart did not return a masked-ID string")
    cart_path = "/rest/V1/guest-carts/" + quote_path(token)

    added_items = []
    for sku, quantity in selected:
        add = http.request(
            "POST",
            origin + cart_path + "/items",
            headers={"Content-Type": "application/json"},
            json={"cartItem": {"sku": sku, "qty": quantity, "quote_id": token}},
        )
        denied = _denied(
            "quote",
            "/rest/V1/guest-carts/[redacted]/items",
            add,
            "guest cart item API refused",
        )
        if denied:
            return denied
        if add.status_code not in {200, 201}:
            raise ToolError(
                f"Magento rejected SKU {sku!r} with HTTP {add.status_code}"
            )
        added = json_object(add, "Magento add item")
        if added.get("sku") != sku:
            raise ToolError("Magento add item did not return the selected SKU")
        added_quantity = added.get("qty")
        if (
            isinstance(added_quantity, bool)
            or not isinstance(added_quantity, (int, float))
            or Decimal(str(added_quantity)) != quantity
        ):
            raise ToolError(
                f"Magento add item did not return the requested quantity {quantity}"
            )
        added_items.append({"sku": sku, "title": added.get("name"), "quantity": quantity})

    totals_response = http.request("GET", origin + cart_path + "/totals")
    denied = _denied(
        "quote",
        "/rest/V1/guest-carts/[redacted]/totals",
        totals_response,
        "guest cart totals API refused",
    )
    if denied:
        return denied
    if totals_response.status_code != 200:
        raise ToolError(
            f"Magento guest cart totals returned HTTP {totals_response.status_code}"
        )
    totals = json_object(totals_response, "Magento guest cart totals")
    quote_currency = totals.get("quote_currency_code")
    base_currency = totals.get("base_currency_code")
    subtotal_value = totals.get("subtotal")
    base_subtotal_value = totals.get("base_subtotal")
    if (
        not isinstance(quote_currency, str)
        or not quote_currency
        or not isinstance(base_currency, str)
        or not base_currency
        or subtotal_value is None
    ):
        raise ToolError("Magento guest cart totals has no subtotal and currency codes")
    subtotal = money(subtotal_value, quote_currency)

    rates_response = http.request(
        "POST",
        origin + cart_path + "/estimate-shipping-methods",
        headers={"Content-Type": "application/json"},
        json={"address": destination_for("magento", destination)},
    )
    denied = _denied(
        "quote",
        "/rest/V1/guest-carts/[redacted]/estimate-shipping-methods",
        rates_response,
        "shipping estimate API refused",
    )
    if denied:
        return denied
    if rates_response.status_code != 200:
        raise ToolError(
            f"Magento shipping estimate returned HTTP {rates_response.status_code}"
        )
    raw_rates = json_list(rates_response, "Magento shipping estimate")
    options = [_rate_option(rate, quote_currency, base_currency) for rate in raw_rates]
    return quote_outcome(
        MagentoQuote(
            item={"lines": added_items},
            base_subtotal=(
                money(base_subtotal_value, base_currency)
                if base_subtotal_value is not None
                else None
            ),
            subtotal_incl_tax=(
                money(totals["subtotal_incl_tax"], quote_currency)
                if totals.get("subtotal_incl_tax") is not None
                else None
            ),
        ),
        options,
        subtotal,
        no_quote_reason="empty_rate_list"
        if not raw_rates
        else "no_comparable_delivery_rate",
    )


def quote_path(value: str) -> str:
    return url_quote(value, safe="")


def _graphql_detail(
    http: Http,
    origin: str,
    sku: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    response = http.request(
        "POST",
        origin + "/graphql",
        headers={"Content-Type": "application/json"},
        json={"query": DETAIL_QUERY, "variables": {"sku": sku}},
    )
    if response.status_code in {401, 403, 404}:
        return None, [{"stage": "detail", "sku": sku, "status": response.status_code}]
    if response.status_code != 200:
        raise ToolError(f"Magento GraphQL detail returned HTTP {response.status_code}")
    envelope = json_object(response, "Magento GraphQL detail")
    errors = _graphql_errors(envelope, "detail", sku)
    if _products(envelope) is None:
        if errors:
            return None, errors
        raise ToolError("Magento GraphQL detail has no products data")
    return envelope, errors


def _graphql_errors(
    envelope: dict[str, Any], stage: str, sku: str | None = None
) -> list[dict[str, Any]]:
    errors = envelope.get("errors", [])
    if not isinstance(errors, list):
        raise ToolError("Magento GraphQL errors must be an array")
    normalized = []
    for error in errors[:20]:
        if not isinstance(error, dict):
            raise ToolError("Magento GraphQL error must be an object")
        normalized.append(
            {
                "stage": stage,
                "message": str(error.get("message", "GraphQL error"))[:240],
                "path": error.get("path"),
                **({"sku": sku} if sku else {}),
            }
        )
    return normalized


def _products(envelope: dict[str, Any]) -> dict[str, Any] | None:
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    products = data.get("products")
    return products if isinstance(products, dict) else None


def _detail_items(
    envelope: dict[str, Any],
    selected_sku: str,
    entry_url: str,
) -> tuple[list[dict[str, Any]], int]:
    products = _products(envelope)
    rows = products.get("items") if products else None
    if not isinstance(rows, list):
        raise ToolError("Magento GraphQL detail items must be an array")
    parent_matches = [
        row for row in rows if isinstance(row, dict) and row.get("sku") == selected_sku
    ]
    if len(parent_matches) > 1:
        raise ToolError("Magento selected top-level SKU resolved more than once")
    selected_variant = None
    if parent_matches:
        parent = parent_matches[0]
    else:
        child_matches = []
        for row in rows:
            if not isinstance(row, dict):
                raise ToolError(
                    "Magento GraphQL detail product has an unexpected shape"
                )
            variants = row.get("variants")
            if row.get("__typename") != "ConfigurableProduct" or not isinstance(
                variants, list
            ):
                continue
            for variant in variants:
                if not isinstance(variant, dict) or not isinstance(
                    variant.get("product"), dict
                ):
                    raise ToolError(
                        "Magento configurable variant has an unexpected shape"
                    )
                if variant["product"].get("sku") == selected_sku:
                    child_matches.append((row, variant))
        if not child_matches:
            raise DetailSkuNotFound(
                "Magento SKU detail did not contain the selected SKU"
            )
        if len(child_matches) > 1:
            raise ToolError(
                "Magento selected configurable child SKU resolved more than once"
            )
        parent, selected_variant = child_matches[0]
    data = envelope.get("data")
    config = data.get("storeConfig") if isinstance(data, dict) else None
    if not isinstance(config, dict):
        raise ToolError("Magento GraphQL detail has no storeConfig")
    if "product_url_suffix" not in config:
        raise ToolError("Magento GraphQL detail has no storeConfig.product_url_suffix")
    raw_suffix = config["product_url_suffix"]
    if raw_suffix is not None and not isinstance(raw_suffix, str):
        raise ToolError(
            "Magento GraphQL detail storeConfig.product_url_suffix must be "
            "a string or null"
        )
    suffix = raw_suffix or ""
    url_key = parent.get("url_key")
    if not isinstance(url_key, str) or not url_key:
        raise ToolError("Magento GraphQL detail product has no product url_key")
    product_url = canonical_url(urljoin(entry_url, url_key + suffix))
    if selected_variant is not None:
        child = selected_variant["product"]
        if child.get("__typename") != "SimpleProduct":
            raise ToolError(
                "Magento selected configurable child is not a simple product"
            )
        attributes = (
            selected_variant.get("attributes")
            if isinstance(selected_variant.get("attributes"), list)
            else []
        )
        return [_graphql_item(child, parent, product_url, attributes)], 0
    if parent.get("__typename") == "SimpleProduct":
        return [_graphql_item(parent, parent, product_url, [])], 0
    variants = parent.get("variants")
    if parent.get("__typename") != "ConfigurableProduct" or not isinstance(
        variants, list
    ):
        return [], 1
    items = []
    for variant in variants:
        if not isinstance(variant, dict) or not isinstance(
            variant.get("product"), dict
        ):
            raise ToolError("Magento configurable variant has an unexpected shape")
        child = variant["product"]
        if child.get("__typename") != "SimpleProduct":
            continue
        attributes = (
            variant.get("attributes")
            if isinstance(variant.get("attributes"), list)
            else []
        )
        items.append(_graphql_item(child, parent, product_url, attributes))
    return items, int(not items)


def _graphql_item(
    product: dict[str, Any],
    parent: dict[str, Any],
    product_url: str,
    attributes: list[Any],
) -> dict[str, Any]:
    sku = product.get("sku")
    if not isinstance(sku, str) or not sku:
        raise ToolError("Magento product has no simple SKU")
    final, regular = _prices(product)
    labels = [
        str(value.get("label"))
        for value in attributes
        if isinstance(value, dict) and value.get("label")
    ]
    description_value = parent.get("short_description") or parent.get("description")
    media = parent.get("media_gallery", [])
    return {
        "item_ref": item_ref("magento", {"sku": sku}),
        "title": parent.get("name") or product.get("name"),
        "description": description_value.get("html", "") if isinstance(description_value, dict) else "",
        "image_urls": [value["url"] for value in media if isinstance(value, dict) and isinstance(value.get("url"), str)],
        "variant": ", ".join(labels)
        or (product.get("name") if product is not parent else None),
        "sku": sku,
        "barcode": None,
        "available": product.get("stock_status") == "IN_STOCK",
        "price": final,
        "compare_at_price": regular if regular != final else None,
        "weight": None,
        "url": product_url,
        "options": [
            {
                "code": value.get("code"),
                "label": value.get("label"),
                "value": value.get("value_index"),
            }
            for value in attributes
            if isinstance(value, dict)
        ],
    }


def _prices(
    product: dict[str, Any],
) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    price_range = product.get("price_range")
    minimum = (
        price_range.get("minimum_price") if isinstance(price_range, dict) else None
    )
    final = minimum.get("final_price") if isinstance(minimum, dict) else None
    regular = minimum.get("regular_price") if isinstance(minimum, dict) else None
    return _price(final), _price(regular)


def _price(value: Any) -> dict[str, str] | None:
    if (
        not isinstance(value, dict)
        or value.get("value") is None
        or value.get("currency") is None
    ):
        return None
    return money(value["value"], value["currency"])


def _html_search(
    http: Http,
    detection: MagentoDetectedStore,
    query: str,
    limit: int,
) -> dict[str, object]:
    response = http.get(
        http.magento_html_search(detection.origin, detection.entry_url, query)
    )
    if response.status_code in {301, 302, 303, 307, 308}:
        location = response.headers.get("location")
        if not location:
            raise ToolError("Magento HTML search redirect has no Location header")
        target = canonical_url(urljoin(str(response.url), location))
        if url_origin(target) != detection.origin:
            raise ToolError("Magento HTML search redirect must stay on the same storefront")
        response = http.request("GET", target)
    denied = _denied(
        "search",
        "/catalogsearch/result",
        response,
        "public Magento HTML search refused",
    )
    if denied:
        return denied
    if response.status_code != 200:
        raise ToolError(f"Magento HTML search returned HTTP {response.status_code}")
    parser = ProductLinks()
    parser.feed(response.text)
    links: list[tuple[str, str]] = []
    product_urls: set[str] = set()
    for href in parser.links:
        candidate = _canonical_product_url(urljoin(str(response.url), href))
        if candidate is None:
            continue
        if url_origin(candidate) == detection.origin and candidate not in product_urls:
            product_urls.add(candidate)
            links.append((candidate, candidate))
        if len(links) == MAX_SEARCH_RESULTS:
            break

    items: list[dict[str, Any]] = []
    omitted = 0
    for product_url, raw_href in links:
        product = http.get(
            http.discovered_product_page(response, raw_href, detection.origin)
        )
        if product.status_code == 404:
            continue
        denied = _denied(
            "search", product_url, product, "public Magento product page refused"
        )
        if denied:
            return denied
        if product.status_code != 200:
            raise ToolError(f"Magento product page returned HTTP {product.status_code}")
        page = ProductPage()
        page.feed(product.text)
        page_items, configured = _page_items(page, product_url)
        items.extend(page_items)
        omitted += int(configured and not page_items)
    return search_result(
        MagentoSearch(
            source="html",
            api_errors=(),
            configurable_products_omitted=omitted,
        ),
        query,
        _unique_skus(items)[:limit],
    )


def _page_items(
    page: ProductPage, product_url: str
) -> tuple[list[dict[str, Any]], bool]:
    standalone_items: list[dict[str, Any]] = []
    variant_items: list[dict[str, Any]] = []
    for raw in page.json_ld:
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in _walk_json(document):
            if not isinstance(node, dict):
                continue
            schema_type = _schema_type(node.get("@type"))
            if schema_type == "productgroup" and isinstance(
                node.get("hasVariant"), list
            ):
                parent = node.get("name")
                for child in node["hasVariant"]:
                    if isinstance(child, dict):
                        normalized = _json_ld_item(child, parent, product_url)
                        if normalized:
                            variant_items.append(normalized)
            if schema_type == "product":
                normalized = _json_ld_item(node, None, product_url)
                if normalized:
                    standalone_items.append(normalized)
    configured = bool(page.required_fields)
    items = _unique_skus(
        variant_items if configured else variant_items + standalone_items
    )
    if items:
        return items, configured
    if page.form_sku and page.itemprop_sku and page.form_sku != page.itemprop_sku:
        raise ToolError("Magento product page form and visible SKU disagree")
    sku = page.form_sku or page.itemprop_sku
    if not sku or configured:
        return [], configured
    price = (
        money(page.price, page.currency)
        if page.price is not None and page.currency is not None
        else None
    )
    return [
        {
            "item_ref": item_ref("magento", {"sku": sku}),
            "title": page.title,
            "variant": None,
            "sku": sku,
            "barcode": None,
            "available": page.available,
            "price": price,
            "compare_at_price": None,
            "weight": None,
            "url": product_url,
            "options": [],
        }
    ], False


def _json_ld_item(
    product: dict[str, Any], parent_name: Any, product_url: str
) -> dict[str, Any] | None:
    sku = product.get("sku")
    if not isinstance(sku, str) or not sku:
        return None
    offers = product.get("offers")
    offer = (
        offers
        if isinstance(offers, dict)
        else offers[0]
        if isinstance(offers, list) and len(offers) == 1
        else {}
    )
    price = None
    if (
        isinstance(offer, dict)
        and offer.get("price") is not None
        and offer.get("priceCurrency") is not None
    ):
        price = money(offer["price"], offer["priceCurrency"])
    availability = offer.get("availability") if isinstance(offer, dict) else None
    available = (
        str(availability).lower().rstrip("/").endswith("instock")
        if availability is not None
        else None
    )
    barcode = next(
        (
            product[name]
            for name in ("gtin14", "gtin13", "gtin12", "gtin", "mpn")
            if product.get(name)
        ),
        None,
    )
    raw_images = product.get("image", [])
    images = raw_images if isinstance(raw_images, list) else [raw_images]
    return {
        "item_ref": item_ref("magento", {"sku": sku}),
        "title": parent_name or product.get("name"),
        "description": product.get("description", ""),
        "image_urls": [value for value in images if isinstance(value, str)],
        "variant": product.get("name") if parent_name else None,
        "sku": sku,
        "barcode": barcode,
        "available": available,
        "price": price,
        "compare_at_price": None,
        "weight": None,
        "url": product_url,
        "options": [],
    }


def _schema_type(value: Any) -> str:
    candidate = value[0] if isinstance(value, list) and value else value
    return str(candidate).lower().rstrip("/").rsplit("/", 1)[-1]


def _walk_json(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _unique_skus(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {}
    for product in items:
        sku = product.get("sku")
        if not isinstance(sku, str):
            raise ToolError("Magento normalized product has no SKU")
        unique.setdefault(sku, product)
    return list(unique.values())


def _canonical_product_url(value: str) -> str | None:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.fragment:
        return None
    if any(
        key.casefold() != "tracking"
        for key, _ in parse_qsl(parts.query, keep_blank_values=True)
    ):
        return None
    return canonical_url(
        urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))
    )


def _denied(
    operation: str,
    endpoint: str,
    response: Any,
    reason: str,
) -> dict[str, Any] | None:
    if response.status_code not in {401, 403, 404, 405}:
        return None
    system = wall_system(response)
    if system:
        return bot_wall(operation, "magento", response, system)
    return gated(operation, "magento", endpoint, response, reason)


def _rate_option(rate: Any, quote_currency: str, base_currency: str) -> dict[str, Any]:
    if not isinstance(rate, dict):
        raise ToolError("Magento shipping rate must be an object")
    carrier = rate.get("carrier_code")
    method = rate.get("method_code")
    if (
        not isinstance(carrier, str)
        or not carrier
        or not isinstance(method, str)
        or not method
    ):
        raise ToolError("Magento shipping rate has no carrier and method codes")
    option_id = f"{carrier}/{method}"
    title = (
        " — ".join(
            str(value)
            for value in (rate.get("carrier_title"), rate.get("method_title"))
            if value
        )
        or option_id
    )
    label = f"{option_id} {title}".lower()
    error = str(rate["error_message"]) if rate.get("error_message") else None
    available = rate.get("available")
    if not isinstance(available, bool):
        raise ToolError("Magento shipping rate available must be a boolean")
    amount_value = (
        rate.get("price_incl_tax")
        if rate.get("price_incl_tax") is not None
        else rate.get("amount")
    )
    amount = money(amount_value, quote_currency) if amount_value is not None else None
    zero_amount = amount is not None and Decimal(amount["amount"]) == 0
    if not available or error:
        disposition = "unavailable"
    elif any(
        marker in label
        for marker in (
            "pickup",
            "pick up",
            "click and collect",
            "collect in store",
            "instore",
        )
    ):
        disposition = "pickup"
    elif (
        "paid later" in label
        or "quote later" in label
        or ("freight" in label and zero_amount)
    ):
        disposition = "paid_later"
    elif "fallback" in label:
        disposition = "fallback"
    else:
        disposition = "delivery"
    return shipping_option(
        MagentoShipping(
            carrier_code=carrier,
            method_code=method,
            available=available,
            error=error,
            base_amount=(
                money(rate["base_amount"], base_currency)
                if rate.get("base_amount") is not None
                else None
            ),
            price_excl_tax=(
                money(rate["price_excl_tax"], quote_currency)
                if rate.get("price_excl_tax") is not None
                else None
            ),
            price_incl_tax=(
                money(rate["price_incl_tax"], quote_currency)
                if rate.get("price_incl_tax") is not None
                else None
            ),
        ),
        option_id,
        title,
        disposition,
        amount,
    )
