from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from html import unescape
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx

Platform = Literal[
    "shopify", "woocommerce", "magento", "bigcommerce", "squarespace",
    "wix", "ecwid", "sfcc", "aliexpress", "google_shopping", "amazon",
    "ebay", "shopify_global",
]
Operation = Literal["search", "product", "quote"]
Disposition = Literal["delivery", "pickup", "paid_later", "unavailable", "fallback"]
DEFAULT_DESTINATION = {
    "country": "US",
    "region": "CA",
    "city": "San Francisco",
    "address1": "747 Howard St",
    "postal_code": "94103",
}


class ToolError(RuntimeError):
    """Invalid caller input or a violated storefront response contract."""


@dataclass(frozen=True, kw_only=True)
class DetectedStore:
    origin: str
    entry_url: str
    platform: str
    api_origin: str
    evidence: tuple[str, ...]
    kind: Literal["detected"] = field(init=False, default="detected")


@dataclass(frozen=True, kw_only=True)
class MagentoDetectedStore:
    origin: str
    entry_url: str
    api_origin: str
    evidence: tuple[str, ...]
    search_source: Literal["graphql", "html"]
    platform: Literal["magento"] = field(init=False, default="magento")
    kind: Literal["detected"] = field(init=False, default="detected")


@dataclass(frozen=True, kw_only=True)
class UnknownStore:
    origin: str
    entry_url: str
    evidence: tuple[str, ...]
    kind: Literal["unknown"] = field(init=False, default="unknown")


@dataclass(frozen=True, kw_only=True)
class StorefrontBotWall:
    origin: str
    entry_url: str
    evidence: tuple[str, ...]
    system: str
    status: int
    kind: Literal["bot_wall"] = field(init=False, default="bot_wall")


StorefrontDetection = DetectedStore | MagentoDetectedStore | UnknownStore | StorefrontBotWall
PositiveDetection = DetectedStore | MagentoDetectedStore


@dataclass(frozen=True)
class RequestSpec:
    url: str
    params: dict[str, str] | None = None
    follow_redirects: bool = False


class Session:
    """One isolated cookie jar, transport, signer, and evidence log per store worker."""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        signer: Callable[[httpx.Client, httpx.Request], httpx.Response] | None = None,
    ) -> None:
        self.client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(45, connect=10),
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Storefront/1.0)",
                "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            },
            follow_redirects=False,
        )
        self.signer = signer
        self.evidence: list[dict[str, Any]] = []

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, url: str, *, follow_redirects: bool = False, **kwargs: Any) -> httpx.Response:
        request = self.client.build_request(method, url, **kwargs)
        return self._send(request, lambda: self.client.send(request, follow_redirects=follow_redirects))

    def get(self, target: str | RequestSpec) -> httpx.Response:
        spec = target if isinstance(target, RequestSpec) else RequestSpec(target)
        return self.request("GET", spec.url, params=spec.params, follow_redirects=spec.follow_redirects)

    def send_signed(
        self,
        request: httpx.Request,
        sender: Callable[[httpx.Client, httpx.Request], httpx.Response],
    ) -> httpx.Response:
        return self._send(request, lambda: sender(self.client, request))

    def send_shopify(self, request: httpx.Request) -> httpx.Response:
        send = self.signer or (lambda client, value: client.send(value, follow_redirects=False))
        return self._send(request, lambda: send(self.client, request))

    def _send(self, request: httpx.Request, send: Callable[[], httpx.Response]) -> httpx.Response:
        started = time.monotonic()
        try:
            response = send()
        except (httpx.HTTPError, OSError, ValueError) as error:
            raise ToolError(f"{request.method} {redact_url(str(request.url))} failed: {error}") from error
        self.evidence.append({
            "method": request.method,
            "url": redact_url(str(request.url)),
            "status": response.status_code,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        })
        return response

    def storefront_entry(self, url: str, entry_origins: list[str]) -> RequestSpec:
        del entry_origins
        return RequestSpec(normalize_store_url(url), follow_redirects=True)

    def storefront_entry_replay(self, response: httpx.Response) -> RequestSpec:
        return RequestSpec(canonical_url(str(response.url)))

    def detected_entry(self, entry_url: str) -> RequestSpec:
        return RequestSpec(entry_url)

    def woo_products(self, origin: str, query: str, limit: int) -> RequestSpec:
        return RequestSpec(origin + "/wp-json/wc/store/v1/products", {"search": query, "per_page": str(limit)})

    def bigcommerce_search(self, origin: str, query: str) -> RequestSpec:
        return RequestSpec(origin + "/search.php", {"search_query": query})

    def magento_html_search(self, origin: str, entry_url: str, query: str) -> RequestSpec:
        del entry_url
        return RequestSpec(origin + "/catalogsearch/result/", {"q": query})

    def squarespace_search(self, origin: str, query: str) -> RequestSpec:
        return RequestSpec(origin + "/search", {"q": query})

    def wix_bootstrap(self, origin: str) -> RequestSpec:
        return RequestSpec(origin + "/_api/v1/access-tokens")

    def sfcc_search(self, origin: str, entry_url: str, query: str) -> RequestSpec:
        return RequestSpec(urljoin(entry_url, "search"), {"q": query})

    def discovered_product_page(self, response: httpx.Response, raw_reference: str, origin: str) -> RequestSpec:
        target = canonical_url(urljoin(str(response.url), raw_reference))
        if url_origin(target) != origin:
            raise ToolError("Discovered product URL left the storefront origin")
        return RequestSpec(target, follow_redirects=True)

    def squarespace_product_json(self, response: httpx.Response, raw_reference: str, origin: str) -> RequestSpec:
        target = canonical_url(urljoin(str(response.url), raw_reference))
        if url_origin(target) != origin:
            raise ToolError("Squarespace product URL left the storefront origin")
        return RequestSpec(target, {"format": "json"})

    def squarespace_entry_json(self, entry_url: str) -> RequestSpec:
        return RequestSpec(entry_url, {"format": "json"})

    def ecwid_script(self, response: httpx.Response, store_id: str) -> RequestSpec:
        del response
        return RequestSpec("https://app.ecwid.com/script.js", {store_id: ""})

    def ecwid_products(self, response: httpx.Response, store_id: str, token: str, query: str) -> RequestSpec:
        base = str(response.url).split(f"/{store_id}/initial-data", 1)[0]
        return RequestSpec(f"{base}/{store_id}/products", {"token": token, "keyword": query, "limit": "20"})


Http = Session


def normalize_store_url(value: str) -> str:
    url = value if "://" in value else f"https://{value}"
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ToolError("Store must be an absolute HTTPS URL without credentials")
    if parts.query or parts.fragment:
        raise ToolError("Store URL must not contain a query or fragment")
    host = f"[{parts.hostname.lower()}]" if ":" in parts.hostname else parts.hostname.encode("idna").decode().lower()
    try:
        port = "" if parts.port in {None, 443} else f":{parts.port}"
    except ValueError as error:
        raise ToolError("Store URL has an invalid port") from error
    return urlunsplit(("https", host + port, parts.path or "/", "", ""))


def url_origin(value: str) -> str:
    parts = urlsplit(normalize_store_url(value))
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def canonical_url(value: str) -> str:
    parts = urlsplit(value if "://" in value else f"https://{value}")
    return normalize_store_url(urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", "")))


def redact_url(value: str) -> str:
    parts = urlsplit(value)
    path = re.sub(r"(/(?:guest-carts|commerce/cart|checkouts?)/)[^/]+", r"\1[redacted]", parts.path)
    query = urlencode([(name, "[redacted]" if re.sub(r"[^a-z]", "", name.casefold()) in {"apikey", "appkey", "token", "accesstoken"} else item) for name, item in parse_qsl(parts.query)])
    return urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def public_detection(detection: StorefrontDetection, debug: bool = False) -> dict[str, Any]:
    if isinstance(detection, (DetectedStore, MagentoDetectedStore)):
        value: dict[str, Any] = {"platform": detection.platform}
        if debug:
            value |= {"state": "detected", "origin": detection.origin, "api_origin": detection.api_origin, "evidence": list(detection.evidence)}
            if isinstance(detection, MagentoDetectedStore):
                value["search_source"] = detection.search_source
        return value
    value = {"platform": "unknown", "state": detection.kind}
    if debug:
        value |= {"origin": detection.origin, "evidence": list(detection.evidence)}
        if isinstance(detection, StorefrontBotWall):
            value |= {"system": detection.system, "http_status": detection.status}
    return value


def json_object(response: httpx.Response, context: str) -> dict[str, Any]:
    value = _json(response, context)
    if not isinstance(value, dict):
        raise ToolError(f"{context} JSON must be an object")
    return value


def json_list(response: httpx.Response, context: str) -> list[Any]:
    value = _json(response, context)
    if not isinstance(value, list):
        raise ToolError(f"{context} JSON must be an array")
    return value


def _json(response: httpx.Response, context: str) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ToolError(f"{context} did not return JSON") from error


def money(amount: Any, currency: Any) -> dict[str, str]:
    if isinstance(amount, bool) or not isinstance(amount, (str, int, float, Decimal)):
        raise ToolError("Money amount must be numeric")
    if not isinstance(currency, str) or not currency:
        raise ToolError("Money requires a currency")
    try:
        value = Decimal(str(amount))
    except InvalidOperation as error:
        raise ToolError("Money amount must be decimal") from error
    if not value.is_finite():
        raise ToolError("Money amount must be finite")
    return {"amount": format(value, "f"), "currency": currency}


def minor_money(amount: Any, currency: Any, digits: Any) -> dict[str, str]:
    if not isinstance(amount, str) or not re.fullmatch(r"-?\d+", amount):
        raise ToolError("Minor-unit amount must be an integer string")
    if type(digits) is not int or not 0 <= digits <= 4:
        raise ToolError("Currency minor-unit count must be 0 to 4")
    value = Decimal(amount).scaleb(-digits)
    return {"amount": f"{value:.{digits}f}", "currency": str(currency)}


REF_KEYS: dict[str, set[str]] = {
    "shopify": {"variant_id"}, "woocommerce": {"product_id", "product_type", "minimum"},
    "magento": {"sku"}, "bigcommerce": {"product_id", "product_url"},
    "squarespace": {"collection_url", "item_id", "sku"}, "wix": {"product_id"},
    "ecwid": {"product_id", "store_id"}, "sfcc": {"pid"}, "aliexpress": {"product_id"},
    "google_shopping": {"product_id", "merchant_url"}, "amazon": {"asin", "merchant_url"},
    "ebay": {"item_id"}, "shopify_global": {"product_id"},
}


def item_ref(platform: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"platform": platform, **value}


def parse_item_ref(reference: object, platform: str) -> dict[str, Any]:
    if not isinstance(reference, dict) or reference.get("platform") != platform:
        raise ToolError(f"Ref does not belong to {platform}")
    return {key: value for key, value in reference.items() if key not in {"platform", "store"}}


def validate_ref(reference: object) -> dict[str, Any]:
    if not isinstance(reference, dict):
        raise ToolError("A ref must be a JSON object")
    platform = reference.get("platform")
    store = reference.get("store")
    if not isinstance(platform, str) or platform not in REF_KEYS:
        raise ToolError(f"Ref has unknown platform {platform!r}")
    expected = {"platform", "store"} | REF_KEYS[platform]
    unknown = set(reference) - expected
    missing = expected - set(reference)
    if missing:
        raise ToolError(f"{platform} ref is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise ToolError(f"{platform} ref has unknown keys: {', '.join(sorted(unknown))}")
    if not isinstance(store, str) or url_origin(store) != store:
        raise ToolError("Ref store must be a canonical HTTPS origin")
    for key in REF_KEYS[platform]:
        value = reference[key]
        if isinstance(value, bool) or not isinstance(value, (str, int)) or value == "" or value == 0:
            raise ToolError(f"{platform} ref has invalid {key}")
    if platform == "shopify" and not str(reference["variant_id"]).startswith("gid://shopify/ProductVariant/"):
        raise ToolError("shopify ref has invalid variant_id")
    if platform == "woocommerce" and (
        type(reference["product_id"]) is not int
        or type(reference["minimum"]) is not int
        or reference["product_type"] not in {"simple", "variation"}
    ):
        raise ToolError("woocommerce ref has invalid product identity")
    if platform == "bigcommerce" and (
        type(reference["product_id"]) is not int
        or canonical_url(str(reference["product_url"])) != reference["product_url"]
        or url_origin(str(reference["product_url"])) != store
    ):
        raise ToolError("bigcommerce ref has invalid product identity")
    if platform == "squarespace" and (
        canonical_url(str(reference["collection_url"])) != reference["collection_url"]
        or url_origin(str(reference["collection_url"])) != store
    ):
        raise ToolError("squarespace ref has invalid collection_url")
    if platform == "ecwid" and (
        type(reference["product_id"]) is not int
        or not str(reference["store_id"]).isdecimal()
    ):
        raise ToolError("ecwid ref has invalid product identity")
    if platform == "aliexpress" and not str(reference["product_id"]).isdecimal():
        raise ToolError("aliexpress ref has invalid product_id")
    if platform == "shopify_global" and not str(reference["product_id"]).startswith("gid://shopify/p/"):
        raise ToolError("shopify_global ref has invalid product_id")
    return dict(reference)


def canonical_ref(reference: object) -> str:
    return json.dumps(validate_ref(reference), sort_keys=True, separators=(",", ":"))


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_data(self, data: str) -> None:
        self.values.append(data)


def description(value: Any, limit: int) -> str:
    if not isinstance(value, str) or not value:
        return ""
    parser = _Text()
    parser.feed(unescape(value))
    text = " ".join("".join(parser.values).split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def destination_for(platform: str, value: dict[str, str]) -> dict[str, Any]:
    common = {
        "country": value["country"],
        "city": value.get("city", ""),
    }
    if platform == "shopify":
        return {**common, "province": value.get("region", ""), "address1": value.get("address1", ""), "zip": value["postal_code"]}
    if platform == "woocommerce":
        return {"country": value["country"], "state": value.get("region", ""), "city": value.get("city", ""), "address_1": value.get("address1", ""), "postcode": value["postal_code"]}
    if platform == "magento":
        return {**common, "region": value.get("region", ""), "street": [value.get("address1", "")], "postcode": value["postal_code"]}
    if platform == "bigcommerce":
        return {**common, "stateOrProvince": value.get("region", ""), "address1": value.get("address1", ""), "postalCode": value["postal_code"]}
    if platform == "squarespace":
        return {**common, "region": value.get("region", ""), "line1": value.get("address1", ""), "postalCode": value["postal_code"]}
    raise ToolError(f"No destination mapping for {platform}")


def api_error(platform: str, stage: str, reason: str, http_status: int | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"status": "api_error", "platform": platform, "stage": stage, "reason": reason}
    if http_status is not None:
        value["http_status"] = http_status
    return value


def wall_system(response: httpx.Response) -> str | None:
    body = response.text[:200_000].casefold()
    headers = " ".join(f"{key}:{value}" for key, value in response.headers.items()).casefold()
    markers = (("cloudflare", ("cf-ray", "cloudflare", "just a moment")), ("akamai", ("akamai", "reference #")), ("datadome", ("datadome",)), ("captcha", ("captcha", "verify you are human")))
    for name, values in markers:
        if any(value in body or value in headers for value in values):
            return name
    return None


@dataclass(frozen=True, kw_only=True)
class ShopifySearch: platform: str = field(init=False, default="shopify")
@dataclass(frozen=True, kw_only=True)
class WooCommerceSearch: platform: str = field(init=False, default="woocommerce")
@dataclass(frozen=True, kw_only=True)
class MagentoSearch:
    source: str; api_errors: tuple[dict[str, Any], ...]; configurable_products_omitted: int
    platform: str = field(init=False, default="magento")
@dataclass(frozen=True, kw_only=True)
class BigCommerceSearch: platform: str = field(init=False, default="bigcommerce")
@dataclass(frozen=True, kw_only=True)
class SquarespaceSearch:
    discovery: str
    platform: str = field(init=False, default="squarespace")
@dataclass(frozen=True, kw_only=True)
class WixSearch:
    total: int
    platform: str = field(init=False, default="wix")
@dataclass(frozen=True, kw_only=True)
class EcwidSearch:
    store_id: str; total: int
    platform: str = field(init=False, default="ecwid")
@dataclass(frozen=True, kw_only=True)
class SfccSearch:
    endpoint: str
    platform: str = field(init=False, default="sfcc")

@dataclass(frozen=True, kw_only=True)
class ShopifyQuote: platform: str = field(init=False, default="shopify")
@dataclass(frozen=True, kw_only=True)
class WooCommerceQuote:
    cart_totals: dict[str, Any]; cleanup_status: int
    platform: str = field(init=False, default="woocommerce")
@dataclass(frozen=True, kw_only=True)
class MagentoQuote:
    item: dict[str, Any]; base_subtotal: dict[str, str] | None; subtotal_incl_tax: dict[str, str] | None
    platform: str = field(init=False, default="magento")
@dataclass(frozen=True, kw_only=True)
class BigCommerceQuote:
    selected_sku: str | None
    platform: str = field(init=False, default="bigcommerce")
@dataclass(frozen=True, kw_only=True)
class SquarespaceQuote:
    shipping_options_status: str
    platform: str = field(init=False, default="squarespace")

@dataclass(frozen=True, kw_only=True)
class ShopifyShipping:
    code: str | None; description: str | None
    platform: str = field(init=False, default="shopify")
@dataclass(frozen=True, kw_only=True)
class WooCommerceShipping:
    selected: bool; tax: dict[str, str]
    platform: str = field(init=False, default="woocommerce")
@dataclass(frozen=True, kw_only=True)
class MagentoShipping:
    carrier_code: str; method_code: str; available: bool; error: str | None
    base_amount: dict[str, str] | None; price_excl_tax: dict[str, str] | None; price_incl_tax: dict[str, str] | None
    platform: str = field(init=False, default="magento")
@dataclass(frozen=True, kw_only=True)
class BigCommerceShipping:
    transit_time: str | None
    platform: str = field(init=False, default="bigcommerce")
@dataclass(frozen=True, kw_only=True)
class SquarespaceShipping: platform: str = field(init=False, default="squarespace")


def _facts(context: Any) -> dict[str, Any]:
    return {
        key: value
        for key, value in asdict(context).items()
        if key != "platform" and value is not None and value != () and value != 0
    }


def shipping_option(context: Any, option_id: Any, title: Any, disposition: Disposition, amount: dict[str, str] | None) -> dict[str, Any]:
    if not isinstance(option_id, str) or not option_id or not isinstance(title, str) or not title:
        raise ToolError("Shipping option requires an ID and title")
    return {"id": option_id, "title": title, "disposition": disposition, "amount": amount, **_facts(context)}


def search_result(context: Any, query: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "search", "operation": "search", "platform": context.platform, "query": query, "items": items, **_facts(context)}


def quote_outcome(context: Any, options: list[dict[str, Any]], subtotal: dict[str, str], *, no_quote_reason: str = "empty_rate_list") -> dict[str, Any]:
    rates = [{"option_id": item["id"], "title": item["title"], "amount": item["amount"]} for item in options if item.get("disposition") == "delivery"]
    result: dict[str, Any] = {"kind": "quote" if rates else "empty", "operation": "quote", "platform": context.platform, "shipping_options": options, "rates": rates, "subtotal": subtotal, **_facts(context)}
    if not rates:
        result["reason"] = no_quote_reason
    return result


def gated(operation: str, platform: str, endpoint: str, response: httpx.Response, reason: str) -> dict[str, Any]:
    del endpoint
    return api_error(platform, operation, reason, response.status_code)


def bot_wall(operation: str, platform: str, response: httpx.Response, system: str) -> dict[str, Any]:
    return {**api_error(platform, operation, f"{system} challenge", response.status_code), "state": "bot_wall"}


def unsupported_operation(operation: str, platform: str, reason: str, *, browser_required: bool) -> dict[str, Any]:
    return {**api_error(platform, operation, reason), **({"browser_required": True} if browser_required else {})}


def unsupported_configuration(platform: str, fields: list[str], reason: str) -> dict[str, Any]:
    return {**api_error(platform, "quote", reason), "fields": fields, "browser_required": True}


def validate_result(result: dict[str, Any]) -> None:
    if not isinstance(result, dict) or not isinstance(result.get("platform"), str):
        raise ToolError("Adapter result must identify its platform")
