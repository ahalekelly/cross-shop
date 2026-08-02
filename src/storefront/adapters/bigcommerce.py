from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote as url_quote
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

import httpx
from storefront.core import (
    BigCommerceQuote,
    BigCommerceSearch,
    BigCommerceShipping,
    DetectedStore,
    Session,
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
    unsupported_configuration,
    url_origin,
    wall_system,
)

PLATFORM = "bigcommerce"
MAX_PRODUCT_PAGES = 3
def detect(
    response: httpx.Response, origin: str, entry_url: str
) -> DetectedStore | None:
    evidence: list[str] = []
    server = response.headers.get("server", "").casefold()
    source = response.text.casefold()
    if "bigcommerce" in server:
        evidence.append("server=BigCommerce")
    if re.search(r"https?://cdn\d+\.bigcommerce\.com/s-[a-z0-9]+", source):
        evidence.append("BigCommerce CDN store hash")
    if "stencil-utils" in source or "/stencil/" in source:
        evidence.append("BigCommerce Stencil assets")
    if not evidence:
        return None
    return DetectedStore(
        origin=origin,
        entry_url=entry_url,
        platform=PLATFORM,
        api_origin=origin,
        evidence=tuple(evidence),
    )


@dataclass(frozen=True)
class SearchLink:
    href: str
    title: str | None
    sku: str | None
    text: str


@dataclass(frozen=True)
class ProductPage:
    product_id: int
    title: str
    description: str
    image_urls: tuple[str, ...]
    configuration_fields: tuple[str, ...]
    attributes: dict[str, Any]


class SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[SearchLink] = []
        self._link: tuple[str, str | None, str | None] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or self._link is not None:
            return
        values = dict(attrs)
        href = values.get("href")
        if not href:
            return
        if (values.get("data-button-type") or "").casefold() == "add-cart":
            return
        product_metadata = " ".join(
            str(values.get(name, ""))
            for name in ("data-event-type", "data-card-type")
        ).casefold()
        product_identity = any(
            values.get(name)
            for name in ("data-pid", "data-product-id", "data-sku", "data-custom-sku")
        )
        if (
            "searchid=" not in href.casefold()
            and "product" not in product_metadata
            and not product_identity
        ):
            return
        self._link = (
            href,
            values.get("title"),
            values.get("data-sku") or values.get("data-custom-sku"),
        )
        self._text = []

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._link is None:
            return
        href, title, sku = self._link
        self.links.append(
            SearchLink(href, title, sku, " ".join("".join(self._text).split()))
        )
        self._link = None
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._link is not None:
            self._text.append(data)


class ProductParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.product_id: int | None = None
        self.configuration_fields: set[str] = set()
        self.title: str | None = None
        self.description = ""
        self.image_urls: list[str] = []
        self._in_title = False
        self._title_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "h1" and self.title is None:
            self._in_title = True
            self._title_text = []
        property_name = (values.get("property") or values.get("name") or "").casefold()
        content = values.get("content")
        if property_name in {"description", "og:description"} and content and not self.description:
            self.description = content
        if property_name == "og:image" and content:
            self.image_urls.append(content)
        if tag == "img" and values.get("data-image-gallery-main") is not None and values.get("src"):
            self.image_urls.append(values["src"] or "")
        name = values.get("name") or ""
        if tag == "input":
            if name == "product_id" and values.get("value"):
                raw = values["value"] or ""
                if raw.isdecimal() and int(raw) > 0:
                    self.product_id = int(raw)
            elif (
                name.startswith("attribute[")
                and (values.get("type") or "text").lower() != "hidden"
            ):
                self.configuration_fields.add(name)
        elif tag in {"select", "textarea"} and name.startswith("attribute["):
            self.configuration_fields.add(name)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._in_title:
            value = " ".join("".join(self._title_text).split())
            self.title = value or None
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_text.append(data)


class BigCommerce:
    platform = PLATFORM

    def search(self, http: Session, detection: DetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del destination
        return _search(http, detection, query, limit)

    def product(self, http: Session, detection: DetectedStore, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del detection, destination
        reference = item.get("ref")
        url = item.get("url") or (reference.get("product_url") if reference is not None else None)
        if url is None:
            return api_error(
                PLATFORM,
                "product",
                f"{PLATFORM} cannot resolve this input to live exact product detail",
            )
        response = http.request("GET", url, follow_redirects=True)
        if response.status_code != 200:
            return api_error(PLATFORM, "product", "BigCommerce product page failed", response.status_code)
        detail = _product(response.text, canonical_url(str(response.url)))
        if reference is not None and detail["item_ref"]["product_id"] != reference.get("product_id"):
            return api_error(
                PLATFORM,
                "product",
                "BigCommerce product page identity no longer matches the ref",
            )
        return detail

    def quote(self, http: Session, detection: DetectedStore, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        return _quote(http, detection, lines, destination)


def _search(http: Session, detection: DetectedStore, query: str, limit: int) -> dict[str, object]:
    response = http.get(http.bigcommerce_search(detection.origin, query))
    terminal = _terminal(
        "search", "/search.php", response, "public HTML search refused"
    )
    if terminal:
        return terminal
    if response.status_code != 200:
        raise ToolError(f"BigCommerce search returned HTTP {response.status_code}")

    parser = SearchParser()
    parser.feed(response.text)
    ranked: dict[str, tuple[int, int, str]] = {}
    for index, link in enumerate(parser.links):
        try:
            product_reference = _search_product_url(
                urljoin(str(response.url), link.href)
            )
            url = canonical_url(product_reference)
        except ToolError:
            continue
        if url_origin(url) != detection.origin:
            continue
        rank = _rank(query, link)
        if url not in ranked or rank < ranked[url][0]:
            ranked[url] = (rank, index, product_reference)

    items: list[dict[str, Any]] = []
    for url, (_, _, raw_href) in sorted(
        ranked.items(), key=lambda item: item[1]
    )[:MAX_PRODUCT_PAGES]:
        page = http.get(
            http.discovered_product_page(response, raw_href, detection.origin)
        )
        terminal = _terminal("search", url, page, "public product page refused")
        if terminal:
            return terminal
        if page.status_code != 200:
            raise ToolError(
                f"BigCommerce product page returned HTTP {page.status_code}"
            )
        final_url = canonical_url(str(page.url))
        if url_origin(final_url) != detection.origin:
            raise ToolError(
                "BigCommerce product page redirected off the detected storefront"
            )
        items.append(_product(page.text, final_url))
    return search_result(BigCommerceSearch(), query, items[:limit])


def _quote(
    http: Session,
    detection: DetectedStore,
    lines: list[dict[str, Any]],
    destination: dict[str, str],
) -> dict[str, object]:
    http.client.cookies.clear()
    selected = []
    for line in lines:
        reference = parse_item_ref(line["ref"], PLATFORM)
        if set(reference) != {"product_id", "product_url"}:
            raise ToolError("BigCommerce item_ref has unexpected fields")
        product_id = reference["product_id"]
        product_url = reference["product_url"]
        if (
            type(product_id) is not int
            or product_id <= 0
            or not isinstance(product_url, str)
        ):
            raise ToolError("BigCommerce item_ref has invalid product identity")
        if (
            canonical_url(product_url) != product_url
            or url_origin(product_url) != detection.origin
        ):
            raise ToolError(
                "BigCommerce item_ref product URL must be canonical and on the detected storefront"
            )
        page = http.request("GET", product_url, follow_redirects=True)
        terminal = _terminal("quote", product_url, page, "public product page refused")
        if terminal:
            return terminal
        if page.status_code != 200:
            raise ToolError(f"BigCommerce product page returned HTTP {page.status_code}")
        if canonical_url(str(page.url)) != product_url:
            raise ToolError(
                "BigCommerce item_ref product URL no longer resolves to the selected product"
            )
        product = _parse_product(page.text)
        if product.product_id != product_id:
            raise ToolError(
                "BigCommerce item_ref product ID does not match its product URL"
            )
        if product.configuration_fields:
            return unsupported_configuration(
                PLATFORM,
                list(product.configuration_fields),
                "buyer choices are required and are never guessed",
            )
        if (
            product.attributes.get("instock") is not True
            or product.attributes.get("purchasable") is not True
        ):
            raise ToolError("BigCommerce selected product is not currently purchasable")
        selected.append((product_id, line["quantity"]))

    http.client.cookies.clear()
    created = http.request(
        "POST",
        detection.origin + "/api/storefront/carts",
        json={
            "lineItems": [
                {"quantity": quantity, "productId": product_id, "optionSelections": []}
                for product_id, quantity in selected
            ]
        },
    )
    terminal = _terminal(
        "quote",
        "/api/storefront/carts",
        created,
        "anonymous Storefront cart API refused",
    )
    if terminal:
        return terminal
    if created.status_code != 200:
        raise ToolError(
            f"BigCommerce Storefront cart creation returned HTTP {created.status_code}"
        )
    cart = json_object(created, "BigCommerce Storefront cart")
    cart_id = cart.get("id")
    currency = cart.get("currency")
    currency_code = currency.get("code") if isinstance(currency, dict) else None
    if (
        not isinstance(cart_id, str)
        or not cart_id
        or not isinstance(currency_code, str)
    ):
        raise ToolError("BigCommerce Storefront cart omitted its cart ID or currency")
    line_items = cart.get("lineItems")
    physical = line_items.get("physicalItems") if isinstance(line_items, dict) else None
    if not isinstance(physical, list):
        raise ToolError("BigCommerce Storefront cart omitted its physical item array")
    consign_items = []
    for product_id, quantity in selected:
        matches = [
            item
            for item in physical
            if isinstance(item, dict)
            and type(item.get("productId")) is int
            and item["productId"] == product_id
        ]
        if len(matches) != 1:
            raise ToolError(
                "BigCommerce Storefront cart did not contain exactly the selected products"
            )
        item_id = matches[0].get("id")
        if (
            not isinstance(item_id, str)
            or not item_id
            or matches[0].get("isShippingRequired") is not True
        ):
            raise ToolError(
                "BigCommerce selected cart item is not a physical shippable item"
            )
        consign_items.append({"itemId": item_id, "quantity": quantity})

    consignment = http.request(
        "POST",
        detection.origin
        + f"/api/storefront/checkouts/{url_quote(cart_id, safe='')}/consignments",
        params={"include": "consignments.availableShippingOptions"},
        headers={
            "Cookie": f"SHOP_SESSION_TOKEN={_cookie(http, 'SHOP_SESSION_TOKEN')}",
            "X-SF-CSRF-TOKEN": _cookie(http, "SF-CSRF-TOKEN"),
        },
        json=[{"address": destination_for(PLATFORM, destination), "lineItems": consign_items}],
    )
    terminal = _terminal(
        "quote",
        "/api/storefront/checkouts/[redacted]/consignments",
        consignment,
        "anonymous Storefront consignment API refused",
    )
    if terminal:
        return terminal
    if consignment.status_code != 200:
        raise ToolError(
            f"BigCommerce Storefront consignment returned HTTP {consignment.status_code}"
        )
    checkout = json_object(consignment, "BigCommerce shipping quote")
    consignments = checkout.get("consignments")
    if not isinstance(consignments, list):
        raise ToolError("BigCommerce checkout omitted its consignment array")
    options = []
    for value in consignments:
        raw = value.get("availableShippingOptions") if isinstance(value, dict) else None
        if not isinstance(raw, list):
            raise ToolError("BigCommerce consignment omitted availableShippingOptions")
        options.extend(_shipping_option(item, currency_code) for item in raw)
    return quote_outcome(
        BigCommerceQuote(), options, money(cart.get("baseAmount"), currency_code)
    )


def _search_product_url(value: str) -> str:
    parts = urlsplit(value)
    query = parse_qsl(parts.query, keep_blank_values=True)
    tracking_keys = {"searchid", "search_query"}
    if (
        any(key.casefold() not in tracking_keys for key, _ in query)
        or parts.fragment not in {"", "result"}
    ):
        raise ToolError("BigCommerce search result has identity-bearing URL state")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _rank(query: str, link: SearchLink) -> int:
    expected = " ".join(query.casefold().split())
    values = [
        " ".join(value.casefold().split())
        for value in (link.sku, link.title, link.text)
        if value
    ]
    if expected in values:
        return 0
    if any(value.startswith(expected) for value in values):
        return 1
    if any(expected in value for value in values):
        return 2
    return 3


def _product(page: str, product_url: str) -> dict[str, Any]:
    parsed = _parse_product(page)
    attributes = parsed.attributes
    price = attributes.get("price")
    if price is not None and not isinstance(price, dict):
        raise ToolError("BigCommerce product price has an unexpected type")
    current = price.get("without_tax") if isinstance(price, dict) else None
    if current is not None and not isinstance(current, dict):
        raise ToolError("BigCommerce product current price has an unexpected type")
    purchasable = attributes["purchasable"]
    if purchasable and current is None:
        raise ToolError("BigCommerce product omitted its current price")
    sku = attributes.get("sku")
    if not isinstance(sku, str) or not sku:
        raise ToolError("BigCommerce product omitted its SKU")
    barcode = attributes.get("gtin") or attributes.get("upc")
    if barcode is not None and not isinstance(barcode, str):
        raise ToolError("BigCommerce product barcode has an unexpected type")
    compare = price.get("rrp_without_tax") if isinstance(price, dict) else None
    return {
        "item_ref": item_ref(
            PLATFORM, {"product_id": parsed.product_id, "product_url": product_url}
        ),
        "title": parsed.title,
        "description": parsed.description,
        "image_urls": list(parsed.image_urls),
        "variant": None,
        "sku": sku,
        "barcode": barcode,
        "available": attributes["instock"],
        "purchasable": purchasable,
        "price": money(current.get("value"), current.get("currency"))
        if current is not None
        else None,
        "compare_at_price": money(compare.get("value"), compare.get("currency"))
        if isinstance(compare, dict)
        else None,
        "weight": _weight(attributes.get("weight")),
        "url": product_url,
        "requires_configuration": bool(parsed.configuration_fields),
        "configuration_fields": list(parsed.configuration_fields),
    }


def _parse_product(page: str) -> ProductPage:
    parser = ProductParser()
    parser.feed(page)
    match = re.search(r"\bvar\s+BCData\s*=\s*(\{[^\r\n]*\})\s*;", page)
    if parser.product_id is None or parser.title is None or match is None:
        raise ToolError("BigCommerce product page omitted its ID, title, or BCData")
    try:
        bcdata = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise ToolError("BigCommerce BCData was not valid JSON") from error
    attributes = bcdata.get("product_attributes") if isinstance(bcdata, dict) else None
    if not isinstance(attributes, dict):
        raise ToolError("BigCommerce BCData omitted product_attributes")
    if not isinstance(attributes.get("instock"), bool) or not isinstance(
        attributes.get("purchasable"), bool
    ):
        raise ToolError("BigCommerce BCData omitted purchase availability")
    return ProductPage(
        parser.product_id,
        parser.title,
        parser.description,
        tuple(dict.fromkeys(parser.image_urls)),
        tuple(sorted(parser.configuration_fields)),
        attributes,
    )


def _weight(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or isinstance(value.get("value"), bool)
        or not isinstance(value.get("value"), (int, float))
    ):
        raise ToolError("BigCommerce product weight has an unexpected shape")
    formatted = value.get("formatted")
    if not isinstance(formatted, str) or not formatted.split():
        raise ToolError("BigCommerce product weight omitted its unit")
    raw_unit = formatted.split()[-1].upper()
    units = {
        "G": "GRAMS",
        "GRAM": "GRAMS",
        "GRAMS": "GRAMS",
        "KG": "KILOGRAMS",
        "KGS": "KILOGRAMS",
        "KILOGRAM": "KILOGRAMS",
        "KILOGRAMS": "KILOGRAMS",
        "LB": "POUNDS",
        "LBS": "POUNDS",
        "POUND": "POUNDS",
        "POUNDS": "POUNDS",
        "OZ": "OUNCES",
        "OUNCE": "OUNCES",
        "OUNCES": "OUNCES",
    }
    if raw_unit not in units:
        raise ToolError(f"BigCommerce product uses unknown weight unit {raw_unit!r}")
    return {"value": value["value"], "unit": units[raw_unit]}


def _shipping_option(value: Any, currency: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError("BigCommerce shipping option must be an object")
    option_id = value.get("id")
    title = value.get("description") or value.get("type")
    if not isinstance(option_id, str) or not isinstance(title, str):
        raise ToolError("BigCommerce shipping option omitted its ID or title")
    lowered = title.casefold()
    amount_value = value.get("costAfterDiscount")
    if amount_value is None:
        amount_value = value.get("cost")
    amount = money(amount_value, currency) if amount_value is not None else None
    is_pickup = value.get("isPickup", False)
    if type(is_pickup) is not bool:
        raise ToolError("BigCommerce shipping option isPickup must be boolean")
    if "paid later" in lowered or "quote later" in lowered:
        disposition = "paid_later"
    elif is_pickup or "pickup" in lowered or "pick up" in lowered:
        disposition = "pickup"
    elif "fallback" in lowered:
        disposition = "fallback"
    elif amount is None:
        disposition = "unavailable"
    else:
        disposition = "delivery"
    transit_time = (
        value["transitTime"]
        if isinstance(value.get("transitTime"), str) and value["transitTime"]
        else None
    )
    return shipping_option(
        BigCommerceShipping(transit_time=transit_time),
        option_id,
        title,
        disposition,
        amount,
    )


def _cookie(http: Session, name: str) -> str:
    matches = {
        unquote(value.value) for value in http.client.cookies.jar if value.name == name
    }
    if len(matches) != 1:
        raise ToolError(
            f"BigCommerce Storefront cart must set exactly one {name} cookie"
        )
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
