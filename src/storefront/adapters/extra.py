from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any, ClassVar, Literal
from urllib.parse import urljoin, urlsplit

import httpx
from storefront.core import (
    DetectedStore,
    EcwidSearch,
    Session,
    SfccSearch,
    StorefrontBotWall,
    ToolError,
    WixSearch,
    api_error,
    bot_wall,
    canonical_url,
    gated,
    item_ref,
    json_object,
    money,
    search_result,
    unsupported_operation,
    url_origin,
    wall_system,
)

ExtraPlatform = Literal["wix", "ecwid", "sfcc"]
QUOTE_BOUNDARIES: dict[ExtraPlatform, str] = {
    "wix": "Wix cart references and shipping state are mediated by storefront SDK logic",
    "ecwid": "Ecwid has no generalized public API for a destination-specific guest shipping quote",
    "sfcc": "SFCC guest checkout controllers, CSRF, and middleware are merchant-specific",
}
WIX_ECOMMERCE_APP = "1380b703-ce81-ff05-f115-39571d94dfcd"
ECWID_SCRIPT = re.compile(r"app\.ecwid\.com(?::443)?/script\.js\?(\d+)", re.IGNORECASE)
ECWID_ESCAPED_STORE_ID = re.compile(r'\\?"storeId\\?"\s*:\s*\\?"?(\d+)')
ECWID_API_BASE = re.compile(r'"apiBaseUrl":"([^"]+)"')
MAX_RESULTS = 20


def detect(
    response: httpx.Response, origin: str, entry_url: str
) -> DetectedStore | StorefrontBotWall | None:
    system = wall_system(response)
    if system is not None:
        return StorefrontBotWall(
            origin=origin,
            entry_url=entry_url,
            evidence=(f"HTTP {response.status_code} {system} challenge",),
            system=system,
            status=response.status_code,
        )

    body = response.text[:2_000_000]
    lower = body.lower()
    matches: dict[ExtraPlatform, list[str]] = {"wix": [], "ecwid": [], "sfcc": []}

    if response.headers.get("server", "").casefold() == "pepyaka":
        matches["wix"].append("Server: Pepyaka")
    if "x-wix-request-id" in response.headers:
        matches["wix"].append("x-wix-request-id")
    if "static.wixstatic.com" in lower or "wix-essential-viewer-model" in lower:
        matches["wix"].append("Wix storefront assets")

    if ECWID_SCRIPT.search(body) is not None:
        matches["ecwid"].append("app.ecwid.com/script.js?<storeId>")
    if "storefront-api.ecwid.com" in lower:
        matches["ecwid"].append("Ecwid storefront API")

    if "x-dw-request-base-id" in response.headers:
        matches["sfcc"].append("x-dw-request-base-id")
    if "/on/demandware.static/" in lower or "/on/demandware.store/" in lower:
        matches["sfcc"].append("Demandware storefront routes")
    if "cquotient.siteid" in lower:
        matches["sfcc"].append("CQuotient.siteId")

    platforms = [platform for platform, evidence in matches.items() if evidence]
    if len(platforms) > 1:
        raise ToolError(f"Conflicting storefront signatures: {', '.join(platforms)}")
    if not platforms:
        return None

    platform = platforms[0]
    api_origin = "https://app.ecwid.com" if platform == "ecwid" else origin
    return DetectedStore(
        origin=origin,
        entry_url=entry_url,
        platform=platform,
        api_origin=api_origin,
        evidence=tuple(matches[platform]),
    )


class _BrowserBoundary:
    """Wix, Ecwid, and SFCC publish catalog search but no anonymous exact-detail or cart API."""

    platform: ExtraPlatform

    def product(self, http: Session, detection: DetectedStore, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del http, detection, item, destination
        return api_error(
            self.platform,
            "product",
            f"{self.platform} cannot resolve this input to live exact product detail",
        )

    def quote(self, http: Session, detection: DetectedStore, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        del http, detection, lines, destination
        return unsupported_operation(
            "quote", self.platform, QUOTE_BOUNDARIES[self.platform], browser_required=True
        )


class Wix(_BrowserBoundary):
    platform: ExtraPlatform = "wix"

    def search(self, http: Session, detection: DetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del destination
        _require_query(self.platform, query)
        return _wix_search(http, detection, query, limit)


class Ecwid(_BrowserBoundary):
    platform: ExtraPlatform = "ecwid"

    def search(self, http: Session, detection: DetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del destination
        _require_query(self.platform, query)
        return _ecwid_search(http, detection, query, limit)


class Sfcc(_BrowserBoundary):
    platform: ExtraPlatform = "sfcc"

    def search(self, http: Session, detection: DetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del destination
        _require_query(self.platform, query)
        return _sfcc_search(http, detection, query, limit)


def _require_query(platform: ExtraPlatform, query: str) -> None:
    if not query.strip():
        raise ToolError(f"{platform} search requires a nonempty query")


def _wix_search(http: Session, detection: DetectedStore, query: str, limit: int) -> dict[str, object]:
    origin = _api_origin(detection, "wix")
    token_response = http.get(http.wix_bootstrap(origin))
    terminal = _wall(token_response, "wix")
    if terminal is not None:
        return terminal
    _require_ok(token_response, "Wix anonymous access-token bootstrap")
    token_payload = json_object(token_response, "Wix anonymous access-token bootstrap")
    apps = token_payload.get("apps")
    if not isinstance(apps, dict):
        raise ToolError("Wix anonymous token response has no apps object")
    ecommerce = apps.get(WIX_ECOMMERCE_APP)
    if (
        not isinstance(ecommerce, dict)
        or not isinstance(ecommerce.get("accessToken"), str)
        or not ecommerce["accessToken"]
    ):
        raise ToolError("Wix anonymous token response has no e-commerce access token")

    filter_value = json.dumps({"name": {"$contains": query}}, separators=(",", ":"))
    product_response = http.request(
        "POST",
        origin + "/_api/catalog-reader-server/api/v1/products/query",
        headers={"Authorization": ecommerce["accessToken"]},
        json={
            "query": {"filter": filter_value, "paging": {"limit": 10, "offset": 0}},
            "includeVariants": True,
        },
    )
    terminal = _wall(product_response, "wix")
    if terminal is not None:
        return terminal
    _require_ok(product_response, "Wix product search")
    payload = json_object(product_response, "Wix product search")
    products = payload.get("products")
    total = payload.get("totalResults")
    if not isinstance(products, list) or not _count(total):
        raise ToolError("Wix product search requires products and totalResults")
    return search_result(
        WixSearch(total=total),
        query,
        [_wix_item(detection, product) for product in products][:limit],
    )


def _wix_item(detection: DetectedStore, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError("Wix product must be an object")
    product_id, name, sku = value.get("id"), value.get("name"), value.get("sku")
    if (
        not isinstance(product_id, str)
        or not product_id
        or not isinstance(name, str)
        or not name
    ):
        raise ToolError("Wix product requires nonempty string id and name fields")
    if sku is not None and not isinstance(sku, str):
        raise ToolError("Wix product sku must be a string or null")
    price = value.get("priceData")
    stock = value.get("stock")
    page = value.get("productPageUrl")
    if (
        not isinstance(price, dict)
        or not isinstance(stock, dict)
        or not isinstance(stock.get("inStock"), bool)
        or not isinstance(page, dict)
        or not isinstance(page.get("path"), str)
        or not page["path"].startswith("/")
    ):
        raise ToolError("Wix product is missing price, stock, or product-page data")
    options = _named_values(value.get("productOptions"), "name", "Wix product options")
    custom_fields = _named_values(
        value.get("customTextFields"), "title", "Wix custom text fields"
    )
    current = money(price.get("discountedPrice"), price.get("currency"))
    regular = money(price.get("price"), price.get("currency"))
    media = value.get("media")
    media_items = media.get("items", []) if isinstance(media, dict) else []
    item = {
        "id": product_id,
        "name": name,
        "description": value.get("description", ""),
        "image_urls": [entry["image"]["url"] for entry in media_items if isinstance(entry, dict) and isinstance(entry.get("image"), dict) and isinstance(entry["image"].get("url"), str)],
        "sku": sku,
        "available": stock["inStock"],
        "price": current,
        "product_url": canonical_url(urljoin(detection.origin + "/", page["path"])),
        "options": options,
        "custom_fields": custom_fields,
        "item_ref": item_ref("wix", {"product_id": product_id}),
    }
    if current != regular:
        item["compare_at_price"] = regular
    return item


def _ecwid_search(
    http: Session, detection: DetectedStore, query: str, limit: int
) -> dict[str, object]:
    _api_origin(detection, "ecwid")
    homepage = http.get(http.detected_entry(detection.entry_url))
    terminal = _wall(homepage, "ecwid")
    if terminal is not None:
        return terminal
    _require_ok(homepage, "Ecwid storefront page")
    store_id = _ecwid_store_id(homepage.text)

    script_response = http.get(http.ecwid_script(homepage, store_id))
    terminal = _wall(script_response, "ecwid")
    if terminal is not None:
        return terminal
    _require_ok(script_response, "Ecwid script.js")
    api_base = _ecwid_api_base(script_response.text)

    initial_response = http.request(
        "POST", f"{api_base}/{store_id}/initial-data", json={"lang": "en"}
    )
    terminal = _wall(initial_response, "ecwid")
    if terminal is not None:
        return terminal
    _require_ok(initial_response, "Ecwid initial data")
    initial = json_object(initial_response, "Ecwid initial data")
    token, currency = _ecwid_public_profile(initial)

    products_response = http.get(http.ecwid_products(store_id, token, query))
    terminal = _wall(products_response, "ecwid")
    if terminal is not None:
        return terminal
    _require_ok(products_response, "Ecwid product search")
    payload = json_object(products_response, "Ecwid product search")
    products, total = payload.get("items"), payload.get("total")
    if not isinstance(products, list) or not _count(total):
        raise ToolError("Ecwid product search requires items and total")
    items = [_ecwid_item(store_id, currency, product) for product in products]
    return search_result(EcwidSearch(store_id=store_id, total=total), query, items[:limit])


def _ecwid_store_id(body: str) -> str:
    match = ECWID_SCRIPT.search(body) or ECWID_ESCAPED_STORE_ID.search(body)
    if match is None:
        raise ToolError("Ecwid store ID was not present in storefront source")
    return match.group(1)


def _ecwid_api_base(body: str) -> str:
    match = ECWID_API_BASE.search(body)
    if match is None:
        raise ToolError("Ecwid script.js has no storefront API base URL")
    value = match.group(1)
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or not parts.hostname.endswith(".ecwid.com")
        or parts.username is not None
        or parts.password is not None
        or parts.path.rstrip("/") != "/storefront/api/v1"
        or parts.query
        or parts.fragment
    ):
        raise ToolError("Ecwid script.js returned an untrusted storefront API base URL")
    return value.rstrip("/")


def _ecwid_public_profile(payload: dict[str, Any]) -> tuple[str, str]:
    profile = payload.get("storeProfile")
    if (
        not isinstance(profile, dict)
        or profile.get("type") != "opened"
        or not isinstance(profile.get("value"), dict)
    ):
        raise ToolError("Ecwid initial data has no opened store profile")
    value = profile["value"]
    try:
        token = value["integrations"]["apps"]["publicTokens"]["ecwid-storefront"]
        currency = value["formats"]["currencyFormat"]["currencyCode"]
    except (KeyError, TypeError) as error:
        raise ToolError(
            "Ecwid store profile has no public token or currency"
        ) from error
    if (
        not isinstance(token, str)
        or not token
        or not isinstance(currency, str)
        or not currency
    ):
        raise ToolError("Ecwid store profile has an invalid public token or currency")
    return token, currency


def _ecwid_item(store_id: str, currency: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError("Ecwid product must be an object")
    product_id, name, sku = value.get("id"), value.get("name"), value.get("sku")
    if (
        isinstance(product_id, bool)
        or not isinstance(product_id, int)
        or product_id <= 0
        or not isinstance(name, str)
        or not name
        or not isinstance(sku, str)
        or not isinstance(value.get("enabled"), bool)
        or not isinstance(value.get("inStock"), bool)
    ):
        raise ToolError(
            "Ecwid product requires id, name, sku, enabled, and inStock fields"
        )
    product_url = value.get("url")
    if not isinstance(product_url, str) or not product_url:
        raise ToolError("Ecwid product requires a public URL")
    options = _named_values(value.get("options"), "name", "Ecwid product options")
    gallery = value.get("galleryImages", [])
    item = {
        "id": product_id,
        "name": name,
        "description": value.get("description", ""),
        "image_urls": [entry["url"] for entry in gallery if isinstance(entry, dict) and isinstance(entry.get("url"), str)],
        "sku": sku,
        "available": value["enabled"] and value["inStock"],
        "price": money(value.get("price"), currency),
        "product_url": _absolute_https_url(product_url, "Ecwid product URL"),
        "options": options,
        "item_ref": item_ref("ecwid", {"product_id": product_id, "store_id": store_id}),
    }
    compare_at = value.get("compareToPrice")
    if compare_at not in {None, 0, "0", "0.0"}:
        item["compare_at_price"] = money(compare_at, currency)
    return item


def _sfcc_search(http: Session, detection: DetectedStore, query: str, limit: int) -> dict[str, object]:
    _api_origin(detection, "sfcc")
    route = canonical_url(urljoin(detection.entry_url, "search"))
    if url_origin(route) != detection.origin:
        raise ToolError("SFCC search route must stay on the detected storefront")
    response = http.get(
        http.sfcc_search(detection.origin, detection.entry_url, query)
    )
    final = urlsplit(str(response.url))
    final_origin = url_origin(f"{final.scheme}://{final.netloc}")
    if final_origin != detection.origin:
        raise ToolError("SFCC search redirected outside the detected storefront")
    terminal = _wall(response, "sfcc")
    if terminal is not None:
        return terminal
    if response.status_code in {401, 403}:
        return gated(
            "search",
            "sfcc",
            route,
            response,
            "public SFCC search refused",
        )
    _require_ok(response, "SFCC search")
    lower = response.text[:2_000_000].lower()
    if (
        "x-dw-request-base-id" not in response.headers
        and "/on/demandware." not in lower
    ):
        raise ToolError("SFCC search response has no SFCC storefront signature")
    parser = SfccProducts(detection.origin)
    parser.feed(response.text)
    parser.close()
    return search_result(
        SfccSearch(endpoint=route), query, parser.items[: min(MAX_RESULTS, limit)]
    )


class SfccProducts(HTMLParser):
    _VOID: ClassVar[set[str]] = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
    _NAME_CLASSES: ClassVar[set[str]] = {"pdp-link", "product-name", "name-link"}

    def __init__(self, origin: str) -> None:
        super().__init__(convert_charrefs=True)
        self.origin = origin
        self.items: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._active: list[tuple[int, str]] = []
        self._names: list[tuple[int, str, list[str]]] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._depth += 1
        values = dict(attrs)
        pid = values.get("data-pid")
        if pid:
            if pid not in self._by_id:
                item = {"id": pid, "item_ref": item_ref("sfcc", {"pid": pid})}
                self._by_id[pid] = item
                self.items.append(item)
            self._active.append((self._depth, pid))

        active_ids = list(dict.fromkeys(active_pid for _, active_pid in self._active))
        classes = set((values.get("class") or "").split())
        if active_ids and classes & self._NAME_CLASSES:
            self._names.extend(
                (self._depth, active_pid, []) for active_pid in active_ids
            )
        for active_pid in active_ids:
            item = self._by_id[active_pid]
            href = values.get("href") if tag == "a" else values.get("data-url")
            if href and "product_url" not in item:
                target = urljoin(self.origin + "/", href)
                parts = urlsplit(target)
                product_origin = url_origin(f"{parts.scheme}://{parts.netloc}")
                if (
                    parts.scheme == "https"
                    and parts.username is None
                    and parts.password is None
                    and not parts.fragment
                    and product_origin == self.origin
                ):
                    item["product_url"] = target
            if (values.get("itemprop") or "").casefold() == "price" and values.get(
                "content"
            ):
                item["_price"] = values["content"]
            if (
                values.get("itemprop") or ""
            ).casefold() == "pricecurrency" and values.get("content"):
                item["_currency"] = values["content"]

        if tag in self._VOID:
            self._close_depth()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self._close_depth()

    def handle_data(self, data: str) -> None:
        for _, _, values in self._names:
            values.append(data)

    def handle_endtag(self, tag: str) -> None:
        del tag
        self._close_depth()

    def close(self) -> None:
        super().close()
        while self._depth:
            self._close_depth()
        for item in self.items:
            raw_price = item.pop("_price", None)
            currency = item.pop("_currency", None)
            if raw_price is not None and currency is not None:
                item["price"] = money(raw_price, currency)

    def _close_depth(self) -> None:
        if self._depth == 0:
            return
        for depth, pid, values in self._names:
            if depth == self._depth:
                name = " ".join("".join(values).split())
                if name and "name" not in self._by_id[pid]:
                    self._by_id[pid]["name"] = name
        self._names = [capture for capture in self._names if capture[0] != self._depth]
        self._active = [active for active in self._active if active[0] != self._depth]
        self._depth -= 1


def _named_values(value: Any, field: str, context: str) -> list[str]:
    if not isinstance(value, list):
        raise ToolError(f"{context} must be an array")
    names: list[str] = []
    for entry in value:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get(field), str)
            or not entry[field]
        ):
            raise ToolError(f"{context} entries require {field}")
        names.append(entry[field])
    return names


def _absolute_https_url(value: str, context: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise ToolError(f"{context} must be absolute HTTPS without credentials")
    return value


def _count(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _api_origin(detection: DetectedStore, platform: ExtraPlatform) -> str:
    if detection.platform != platform:
        raise ToolError(f"{platform} adapter received a different platform detection")
    return detection.api_origin


def _wall(
    response: httpx.Response, platform: ExtraPlatform
) -> dict[str, object] | None:
    system = wall_system(response)
    return None if system is None else bot_wall("search", platform, response, system)


def _require_ok(response: httpx.Response, context: str) -> None:
    if response.status_code != 200:
        raise ToolError(f"{context} returned HTTP {response.status_code}")
