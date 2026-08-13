from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import re
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from cross_shop.core import (
    DetectedStore,
    EtsyQuote,
    Session,
    ToolError,
    api_error,
    bot_wall,
    item_ref,
    json_object,
    minor_money,
    money,
    parse_item_ref,
    quote_outcome,
    shipping_option,
    url_origin,
    wall_system,
)

ALIEXPRESS_URL = "https://api.taobao.com/router/rest"
SERPAPI_URL = "https://serpapi.com/search.json"
EBAY_API = "https://api.ebay.com"
EBAY_TOKEN = "https://api.ebay.com/identity/v1/oauth2/token"
SHOPIFY_GLOBAL_MCP = "https://catalog.shopify.com/api/ucp/mcp"
AMAZON_ORIGIN = "https://www.amazon.com"
AMAZON_AOD_PATH = "/gp/product/ajax/aodAjaxMain/ref=auto_load_aod"
AMAZON_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
ASIN_PATTERN = re.compile(r"[A-Z0-9]{10}")
AMAZON_URL_ASIN_PATTERN = re.compile(
    r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE
)
AMAZON_HOSTS = {"amazon.com", "us.amazon.com", "www.amazon.com"}
WALMART_ORIGIN = "https://www.walmart.com"
WALMART_HOSTS = {"walmart.com", "www.walmart.com"}
WALMART_URL_ID_PATTERN = re.compile(r"/ip/(?:[^/]+/)?(\d+)/?")
BESTBUY_API = "https://api.bestbuy.com/v1"
BESTBUY_FIELDS = (
    "sku,name,salePrice,regularPrice,manufacturer,modelNumber,onlineAvailability,"
    "orderable,url,image,thumbnailImage,customerReviewAverage,customerReviewCount,"
    "shippingCost,freeShipping"
)
ETSY_API = "https://openapi.etsy.com/v3/application"
ETSY_ORIGIN = "https://www.etsy.com"
ETSY_HOSTS = {"etsy.com", "www.etsy.com"}
ETSY_URL_ID_PATTERN = re.compile(r"/listing/(\d+)(?:/.*)?")
SERPAPI_ENGINES = {"google_shopping", "amazon", "walmart"}
ZERO_DIGIT_CURRENCIES = {
    "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW",
    "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
}
THREE_DIGIT_CURRENCIES = {
    "BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND",
}
FOUR_DIGIT_CURRENCIES = {"CLF", "UYW"}


def _ships_to(destination: dict[str, str]) -> dict[str, str]:
    return {
        key: destination[key]
        for key in ("country", "region", "postal_code")
        if key in destination
    }


def _https_url(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise ToolError(f"{context} must be a string")
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise ToolError(f"{context} must be absolute HTTPS without credentials")
    return value


def _ucp_money(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ToolError("Shopify Global Catalog price must be an object")
    amount, currency = value.get("amount"), value.get("currency")
    if (
        isinstance(amount, bool)
        or not isinstance(amount, int)
        or not isinstance(currency, str)
    ):
        raise ToolError(
            "Shopify Global Catalog price requires an integer amount and currency"
        )
    if currency in ZERO_DIGIT_CURRENCIES:
        digits = 0
    elif currency in THREE_DIGIT_CURRENCIES:
        digits = 3
    elif currency in FOUR_DIGIT_CURRENCIES:
        digits = 4
    else:
        digits = 2
    return minor_money(str(amount), currency, digits)


class AliExpress:
    platform = "aliexpress"

    def search(self, session: Session, detection: DetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del detection
        app_key = os.environ.get("ALIEXPRESS_APP_KEY")
        secret = os.environ.get("ALIEXPRESS_APP_SECRET")
        if not app_key or not secret:
            return api_error(self.platform, "search", "Set ALIEXPRESS_APP_KEY and ALIEXPRESS_APP_SECRET")
        params = {
            "app_key": app_key, "format": "json", "keywords": query,
            "method": "aliexpress.affiliate.product.query", "page_no": "1",
            "page_size": str(limit), "ship_to_country": destination["country"],
            "sign_method": "hmac", "target_currency": "USD", "target_language": "EN",
            "timestamp": datetime.datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"), "v": "2.0",
        }
        message = "".join(key + params[key] for key in sorted(params))
        params["sign"] = hmac.new(secret.encode(), message.encode(), hashlib.md5).hexdigest().upper()
        response = session.request("POST", ALIEXPRESS_URL, data=params)
        payload = json_object(response, "AliExpress search")
        if response.status_code != 200 or "error_response" in payload:
            return api_error(self.platform, "search", "AliExpress Affiliate API rejected the search", response.status_code)
        try:
            result = payload["aliexpress_affiliate_product_query_response"]["resp_result"]
            response_code = result["resp_code"]
        except (KeyError, TypeError) as error:
            raise ToolError("AliExpress response omitted resp_result") from error
        if response_code != 200:
            return api_error(
                self.platform,
                "search",
                f"AliExpress Affiliate API returned response code {response_code!r}",
            )
        try:
            products = result["result"]["products"]["product"]
        except (KeyError, TypeError) as error:
            raise ToolError("AliExpress response omitted its products") from error
        if not isinstance(products, list):
            raise ToolError("AliExpress products must be an array")
        items = []
        for product in products:
            if not isinstance(product, dict):
                raise ToolError("AliExpress product must be an object")
            product_id = str(product.get("product_id", ""))
            title = product.get("product_title")
            url = product.get("product_detail_url")
            if not product_id.isdecimal() or not isinstance(title, str) or not title:
                raise ToolError("AliExpress product has invalid identity fields")
            product_url = _https_url(url, "AliExpress product URL")
            price = product.get("target_sale_price")
            currency = product.get("target_sale_price_currency")
            if not isinstance(price, str) or currency != "USD":
                raise ToolError("AliExpress product requires a USD decimal target price")
            image = product.get("product_main_image_url")
            images = [_https_url(image, "AliExpress product image")] if image is not None else []
            items.append({
                "title": title, "price": money(price, currency),
                "product_url": product_url, "image_urls": images,
                "item_ref": item_ref(self.platform, {"product_id": product_id}), "lead": True,
            })
        return {"kind": "search", "platform": self.platform, "items": items}

    def product(self, session: Session, detection: DetectedStore, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del session, detection, item, destination
        return api_error(self.platform, "product", "Affiliate product detail is unavailable; search again")

    def quote(self, session: Session, detection: DetectedStore, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        del session, detection, lines, destination
        return api_error(self.platform, "quote", "Quotes are unavailable through the AliExpress Affiliate API")


class SerpApi:
    def __init__(self, engine: str) -> None:
        if engine not in SERPAPI_ENGINES:
            raise ToolError(f"Unsupported SerpApi engine {engine!r}")
        self.engine = engine
        self.platform = engine

    def search(self, session: Session, detection: DetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del detection, destination
        key = os.environ.get("SERPAPI_API_KEY")
        if not key:
            return api_error(self.platform, "search", "Set SERPAPI_API_KEY")
        params = {"engine": self.engine, "api_key": key} | {
            "google_shopping": {"q": query, "location": "San Francisco, California, United States", "gl": "us", "hl": "en", "direct_link": "true"},
            "amazon": {"k": query, "amazon_domain": "amazon.com", "language": "en_US"},
            "walmart": {"query": query},
        }[self.engine]
        response = session.request("GET", SERPAPI_URL, params=params)
        payload = json_object(response, "SerpApi search")
        if response.status_code != 200 or "error" in payload:
            provider_error = str(payload.get("error", "SerpApi rejected the search"))
            reason = provider_error.replace(key, "[redacted]")
            return api_error(self.platform, "search", reason, response.status_code)
        metadata = payload.get("search_metadata")
        if not isinstance(metadata, dict) or metadata.get("status") != "Success":
            raise ToolError("SerpApi search did not report Success")
        if self.engine == "google_shopping":
            layouts = [
                payload[key]
                for key in ("shopping_results", "inline_shopping_results")
                if key in payload
            ]
            if len(layouts) > 1:
                raise ToolError("SerpApi returned ambiguous shopping result layouts")
            raw = layouts[0] if layouts else []
        else:
            raw = payload.get("organic_results", [])
        if not isinstance(raw, list):
            raise ToolError("SerpApi results must be an array")
        items = [self._item(value) for value in raw[:limit]]
        return {"kind": "search", "platform": self.platform, "items": items}

    def _item(self, value: object) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("title"), str) or not value["title"]:
            raise ToolError("SerpApi result requires a title")
        thumbnail = value.get("thumbnail")
        images = [_https_url(thumbnail, "SerpApi thumbnail")] if thumbnail is not None else []
        if self.engine == "walmart":
            offer = value.get("primary_offer")
            us_item_id = str(value.get("us_item_id", ""))
            if not isinstance(offer, dict) or not us_item_id.isdecimal():
                raise ToolError("SerpApi Walmart result requires us_item_id and primary_offer")
            return {
                "title": value["title"],
                "price": money(offer.get("offer_price"), offer.get("currency")),
                "product_url": f"{WALMART_ORIGIN}/ip/{us_item_id}",
                "image_urls": images,
                "item_ref": item_ref(self.platform, {"us_item_id": us_item_id}),
                "lead": True,
            }
        url = _https_url(
            value.get("direct_link") or value.get("link_clean") or value.get("link") or value.get("product_link"),
            "SerpApi merchant URL",
        )
        price = value.get("extracted_price")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0:
            raise ToolError("SerpApi result requires numeric extracted_price")
        if self.engine == "amazon":
            asin = value.get("asin")
            if not isinstance(asin, str):
                raise ToolError("SerpApi Amazon result requires asin")
            reference = item_ref(self.platform, {"asin": amazon_asin(asin)})
        else:
            product_id = str(value.get("product_id", value.get("position", "")))
            if not product_id:
                raise ToolError("SerpApi Shopping result requires product_id or position")
            reference = item_ref(self.platform, {"product_id": product_id, "merchant_url": url})
        return {"title": value["title"], "price": money(price, "USD"), "product_url": url, "image_urls": images, "item_ref": reference, "lead": True}

    def product(self, session: Session, detection: DetectedStore, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del session, detection, destination
        reason = "Google Shopping results are leads; open the merchant link" if self.engine == "google_shopping" else "No anonymous Amazon product API exists"
        return {**api_error(self.platform, "product", reason), **({"merchant_url": item["url"]} if isinstance(item.get("url"), str) else {})}

    def quote(self, session: Session, detection: DetectedStore, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        del session, detection, lines, destination
        reason = "Quote the merchant storefront from the Google Shopping lead" if self.engine == "google_shopping" else "No anonymous Amazon cart API exists"
        return api_error(self.platform, "quote", reason)


class Amazon:
    platform = "amazon"

    def search(self, session: Session, detection: DetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        return SerpApi("amazon").search(session, detection, query, limit, destination)

    def product(self, session: Session, detection: DetectedStore, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del detection, destination
        reference = item.get("ref")
        source = reference.get("asin") if isinstance(reference, dict) else item.get("url")
        if not isinstance(source, str):
            raise ToolError("Amazon product requires an ASIN ref or product URL")
        asin = amazon_asin(source)
        headers = {
            "User-Agent": AMAZON_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
        bootstrap = session.request("GET", AMAZON_ORIGIN + "/", headers=headers)
        if bootstrap.status_code not in {200, 202}:
            raise ToolError(
                f"Amazon anonymous-session bootstrap returned HTTP {bootstrap.status_code}"
            )
        response = session.request(
            "GET",
            AMAZON_ORIGIN + AMAZON_AOD_PATH,
            params={"asin": asin, "pc": "dp"},
            headers={
                **headers,
                "Accept": "text/html,*/*",
                "Referer": f"{AMAZON_ORIGIN}/dp/{asin}",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if response.status_code == 404:
            return api_error(
                self.platform,
                "product",
                "Amazon all-offers display is unavailable for this ASIN",
                404,
            )
        if response.status_code != 200:
            raise ToolError(
                f"Amazon all-offers endpoint returned HTTP {response.status_code}; "
                "do not retry or add a bypass"
            )
        retrieved_at = datetime.datetime.now(datetime.UTC).isoformat().replace(
            "+00:00", "Z"
        )
        parsed = _amazon_product(asin, response.text, retrieved_at)
        offers = parsed["offers"]
        if not isinstance(offers, dict):
            raise AssertionError("Amazon parser returned invalid offers")
        featured = offers.get("featured")
        price = (
            featured.get("offer", {}).get("price")
            if isinstance(featured, dict) and featured.get("status") == "available"
            else None
        )
        return {
            "title": parsed["title"],
            "product_url": parsed["product_url"],
            "image_urls": [parsed["image_url"]],
            "available": offers.get("status") == "offers",
            "item_ref": item_ref(self.platform, {"asin": asin}),
            **({"price": price} if isinstance(price, dict) else {}),
            "reviews": parsed["reviews"],
            "offers": offers,
            "delivery_scope": parsed["delivery_scope"],
            "retrieved_at": parsed["retrieved_at"],
        }

    def quote(self, session: Session, detection: DetectedStore, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        del session, detection, lines, destination
        return api_error(self.platform, "quote", "No anonymous Amazon cart API exists")


def _marketplace_path(value: str, hosts: set[str], expectation: str) -> str:
    """Marketplace product URLs are accepted only on their own HTTPS hosts."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ToolError(expectation) from error
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise ToolError(expectation)
    return parsed.path


def amazon_asin(product: str) -> str:
    value = product.strip()
    asin = value.upper()
    if ASIN_PATTERN.fullmatch(asin):
        return asin
    path = _marketplace_path(
        value, AMAZON_HOSTS, "product must be a US Amazon ASIN or product URL"
    )
    match = AMAZON_URL_ASIN_PATTERN.search(path + "/")
    if not match:
        raise ToolError("Amazon product URL has no /dp/ or /gp/product/ ASIN")
    return match.group(1).upper()


def walmart_item_id(product: str) -> str:
    value = product.strip()
    if value.isdecimal():
        return value
    expectation = "product must be a Walmart US item ID or /ip/ product URL"
    match = WALMART_URL_ID_PATTERN.fullmatch(
        _marketplace_path(value, WALMART_HOSTS, expectation)
    )
    if match is None:
        raise ToolError("Walmart product URL has no /ip/ item ID")
    return match.group(1)


def etsy_listing_id(product: str) -> str:
    value = product.strip()
    if value.isdecimal():
        return value
    expectation = "product must be an Etsy listing ID or listing URL"
    match = ETSY_URL_ID_PATTERN.fullmatch(
        _marketplace_path(value, ETSY_HOSTS, expectation)
    )
    if match is None:
        raise ToolError("Etsy product URL has no /listing/ ID")
    return match.group(1)


def marketplace_item_url(host: str | None, value: str) -> str | None:
    """Marketplace identity lives in one ID, so tracking query strings are dropped."""
    if host in AMAZON_HOSTS:
        return f"{AMAZON_ORIGIN}/dp/{amazon_asin(value)}"
    if host in WALMART_HOSTS:
        return f"{WALMART_ORIGIN}/ip/{walmart_item_id(value)}"
    if host in ETSY_HOSTS:
        return f"{ETSY_ORIGIN}/listing/{etsy_listing_id(value)}"
    return None


def _amazon_product(asin: str, html: str, retrieved_at: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("#aod-container") is None:
        raise ToolError(
            "Amazon all-offers response omitted #aod-container; blocked or schema changed"
        )
    title = _amazon_text(soup.select_one("#aod-asin-title-text"), "product title")
    image = soup.select_one("#aod-asin-image-id")
    if not isinstance(image, Tag):
        raise ToolError("Amazon all-offers response omitted the product image")
    image_url = image.get("src")
    if not isinstance(image_url, str) or not _amazon_https_url(image_url):
        raise ToolError("Amazon all-offers response had an invalid product image URL")
    count_node = soup.select_one("#aod-total-offer-count")
    if not isinstance(count_node, Tag):
        raise ToolError("Amazon all-offers response omitted the other-offer count")
    count_text = count_node.get("value")
    if not isinstance(count_text, str) or not count_text.isdigit():
        raise ToolError("Amazon all-offers response had an invalid other-offer count")
    reported_count = int(count_text)
    pinned = soup.select_one("#aod-pinned-offer")
    if not isinstance(pinned, Tag):
        raise ToolError("Amazon all-offers response omitted the featured-offer block")
    other_nodes = soup.select("#aod-offer")
    if reported_count < len(other_nodes):
        raise ToolError("Amazon returned more other offers than its reported count")
    featured_price = pinned.select_one(".apex-pricetopay-value")
    no_featured = "No featured offers available" in pinned.get_text(" ", strip=True)
    if featured_price is not None and no_featured:
        raise ToolError("Amazon returned conflicting featured-offer states")
    if featured_price is None and not no_featured:
        raise ToolError("Amazon featured-offer block had no price or unavailable state")
    other_offers = [_amazon_offer(node) for node in other_nodes]
    if featured_price is None and not other_offers:
        offers: dict[str, Any] = {"status": "no_offers"}
    else:
        featured: dict[str, Any]
        if featured_price is None:
            featured = {"status": "unavailable"}
        else:
            featured = {"status": "available", "offer": _amazon_offer(pinned)}
        offers = {
            "status": "offers",
            "featured": featured,
            "reported_other_offer_count": reported_count,
            "other_offers_complete": reported_count == len(other_offers),
            "other_offers": other_offers,
        }
    return {
        "source": "Amazon all-offers display",
        "status": "found",
        "asin": asin,
        "product_url": f"{AMAZON_ORIGIN}/dp/{asin}",
        "title": title,
        "image_url": image_url,
        "reviews": _amazon_reviews(soup),
        "offers": offers,
        "delivery_scope": "anonymous_default_location",
        "retrieved_at": retrieved_at,
    }


def _amazon_offer(node: Tag) -> dict[str, Any]:
    condition = _amazon_text(node.select_one("#aod-offer-heading"), "offer condition")
    price_node = node.select_one(".apex-pricetopay-value")
    if not isinstance(price_node, Tag):
        raise ToolError("Amazon offer omitted its price")
    if node.select_one("form.AodAddToCart") is None:
        raise ToolError("Amazon priced offer had no add-to-cart form")
    sold_by = _amazon_party(node, "#aod-offer-soldBy", "seller")
    ships_from = _amazon_party(node, "#aod-offer-shipsFrom", "shipper")
    deliveries = [
        {
            "price_text": _amazon_attribute(delivery, "data-csa-c-delivery-price"),
            "time": _amazon_attribute(delivery, "data-csa-c-delivery-time"),
            "condition": _amazon_attribute(delivery, "data-csa-c-delivery-condition"),
            "text": _amazon_text(delivery, "delivery promise"),
        }
        for delivery in node.select("[data-csa-c-delivery-price]")
    ]
    return {
        "condition": condition,
        "price": _amazon_price(price_node),
        "ships_from": ships_from,
        "sold_by": sold_by,
        "delivery_promises": deliveries,
    }


def _amazon_price(node: Tag) -> dict[str, str]:
    symbol = _amazon_text(node.select_one(".a-price-symbol"), "price symbol")
    whole_text = _amazon_text(node.select_one(".a-price-whole"), "whole price")
    fraction = _amazon_text(node.select_one(".a-price-fraction"), "fractional price")
    whole = re.sub(r"[,.\s]", "", whole_text)
    if symbol != "$" or not whole.isdigit() or re.fullmatch(r"\d{2}", fraction) is None:
        raise ToolError("Amazon offer had an invalid USD price")
    return {"amount": f"{int(whole)}.{fraction}", "currency": "USD"}


def _amazon_party(node: Tag, selector: str, field: str) -> str:
    block = node.select_one(selector)
    if not isinstance(block, Tag):
        raise ToolError(f"Amazon offer omitted its {field}")
    value = block.select_one(".a-fixed-left-grid-col.a-col-right > a")
    if value is None:
        value = block.select_one(".a-fixed-left-grid-col.a-col-right > span")
    return _amazon_text(value, field)


def _amazon_reviews(soup: BeautifulSoup) -> dict[str, Any]:
    star = soup.select_one("#aod-asin-reviews-star .a-icon-alt")
    count = soup.select_one("#aod-asin-reviews-count-title")
    if star is None and count is None:
        return {"status": "unrated"}
    if star is None or count is None:
        raise ToolError("Amazon returned an incomplete product-rating block")
    star_match = re.fullmatch(r"(\d(?:\.\d)?) out of 5 stars", _amazon_text(star, "rating"))
    count_match = re.fullmatch(r"([\d,]+) ratings", _amazon_text(count, "rating count"))
    if star_match is None or count_match is None:
        raise ToolError("Amazon returned an invalid product-rating block")
    rating = float(star_match.group(1))
    rating_count = int(count_match.group(1).replace(",", ""))
    if not 0 <= rating <= 5:
        raise ToolError("Amazon returned a product rating outside 0–5")
    return {"status": "rated", "rating": rating, "count": rating_count}


def _amazon_text(node: Tag | None, field: str) -> str:
    if not isinstance(node, Tag):
        raise ToolError(f"Amazon response omitted {field}")
    value = node.get_text(" ", strip=True)
    if not value:
        raise ToolError(f"Amazon response had an empty {field}")
    return value


def _amazon_attribute(node: Tag, name: str) -> str:
    value = node.get(name)
    if not isinstance(value, str):
        raise ToolError(f"Amazon delivery promise omitted {name}")
    return value.strip()


def _amazon_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
    )


class Walmart:
    platform = "walmart"

    def search(self, session: Session, detection: DetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        return SerpApi("walmart").search(session, detection, query, limit, destination)

    def product(self, session: Session, detection: DetectedStore, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del detection, destination
        reference = item.get("ref")
        source = reference.get("us_item_id") if isinstance(reference, dict) else item.get("url")
        if not isinstance(source, str):
            raise ToolError("Walmart product requires a us_item_id ref or product URL")
        us_item_id = walmart_item_id(source)
        response = session.request(
            "GET", f"{WALMART_ORIGIN}/ip/{us_item_id}", follow_redirects=True
        )
        if response.status_code == 404:
            return api_error(
                self.platform, "product", "Walmart has no item page for this ID", 404
            )
        product = _walmart_product(response.text)
        if product is None:
            system = wall_system(response)
            if system is not None:
                return bot_wall("product", self.platform, response, system)
            raise ToolError(
                "Walmart item page omitted its __NEXT_DATA__ product; "
                f"schema changed (HTTP {response.status_code})"
            )
        return _walmart_item(product, us_item_id)

    def quote(self, session: Session, detection: DetectedStore, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        del session, detection, lines, destination
        return api_error(
            self.platform,
            "quote",
            "Walmart derives its delivery location from the request IP and no anonymous "
            "call moves it, so a destination shipping quote is unavailable",
        )


def _walmart_product(html: str) -> dict[str, Any] | None:
    script = BeautifulSoup(html, "html.parser").select_one("script#__NEXT_DATA__")
    if script is None:
        return None
    try:
        node: Any = json.loads(script.get_text())
    except json.JSONDecodeError as error:
        raise ToolError("Walmart __NEXT_DATA__ is not JSON") from error
    for key in ("props", "pageProps", "initialData", "data", "product"):
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else None


def _walmart_item(product: dict[str, Any], us_item_id: str) -> dict[str, Any]:
    name = product.get("name")
    if str(product.get("usItemId")) != us_item_id or not isinstance(name, str) or not name:
        raise ToolError("Walmart item page returned another item or omitted its name")
    price_info = product.get("priceInfo")
    current = price_info.get("currentPrice") if isinstance(price_info, dict) else None
    if not isinstance(current, dict):
        raise ToolError("Walmart item omitted its current price")
    availability = product.get("availabilityStatusV2")
    status = availability.get("value") if isinstance(availability, dict) else None
    if not isinstance(status, str):
        raise ToolError("Walmart item omitted its availability status")
    canonical = product.get("canonicalUrl")
    if not isinstance(canonical, str) or not canonical:
        raise ToolError("Walmart item omitted its canonical URL")
    product_url = urljoin(WALMART_ORIGIN, canonical)
    if url_origin(product_url) != WALMART_ORIGIN:
        raise ToolError("Walmart canonical URL left walmart.com")
    image_info = product.get("imageInfo")
    raw_images = image_info.get("allImages", []) if isinstance(image_info, dict) else []
    if not isinstance(raw_images, list):
        raise ToolError("Walmart item images must be an array")
    return {
        "title": name,
        "price": money(current.get("price"), current.get("currencyUnit")),
        "product_url": product_url,
        "image_urls": [
            _https_url(image.get("url"), "Walmart image URL")
            for image in raw_images
            if isinstance(image, dict)
        ],
        "available": status == "IN_STOCK",
        "item_ref": item_ref("walmart", {"us_item_id": us_item_id}),
        "delivery_scope": "anonymous_default_location",
    }


class BestBuy:
    platform = "best_buy"

    def __init__(self, settings: dict[str, Any]) -> None:
        self.credentials = settings.get("bestbuy")

    def _params(self) -> dict[str, str]:
        if not isinstance(self.credentials, dict) or set(self.credentials) != {"api_key"}:
            raise ToolError("Configure settings.bestbuy with api_key")
        key = self.credentials["api_key"]
        if not isinstance(key, str):
            raise ToolError("Best Buy api_key must be a string")
        return {"apiKey": key, "format": "json", "show": BESTBUY_FIELDS}

    def search(self, session: Session, detection: DetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del detection, destination
        terms = "&".join(f"search={quote(term, safe='')}" for term in query.split())
        response = session.request(
            "GET",
            f"{BESTBUY_API}/products(({terms}))",
            params={**self._params(), "pageSize": str(limit)},
        )
        if response.status_code != 200:
            return self._failure("search", response)
        products = json_object(response, "Best Buy search").get("products")
        if not isinstance(products, list):
            raise ToolError("Best Buy search omitted its products array")
        return {"kind": "search", "platform": self.platform, "items": [self._item(value) for value in products]}

    def product(self, session: Session, detection: DetectedStore, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del detection, destination
        reference = item.get("ref")
        sku = str(reference.get("sku", "")) if isinstance(reference, dict) else ""
        if not sku.isdecimal():
            raise ToolError("Best Buy product requires a numeric sku ref")
        response = session.request(
            "GET", f"{BESTBUY_API}/products/{sku}.json", params=self._params()
        )
        if response.status_code == 404:
            return api_error(
                self.platform, "product", "Best Buy has no product with this SKU", 404
            )
        if response.status_code != 200:
            return self._failure("product", response)
        return self._item(json_object(response, "Best Buy product"))

    def _failure(self, stage: str, response: httpx.Response) -> dict[str, Any]:
        if response.status_code == 403 and "validate your API Key" in response.text:
            raise ToolError("Best Buy rejected settings.bestbuy.api_key")
        return api_error(self.platform, stage, "Best Buy API request failed", response.status_code)

    def _item(self, value: object) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not value["name"]:
            raise ToolError("Best Buy product requires a name")
        sku = str(value.get("sku", ""))
        available = value.get("onlineAvailability")
        if not sku.isdecimal() or not isinstance(available, bool):
            raise ToolError("Best Buy product requires a numeric sku and onlineAvailability")
        image = value.get("image")
        free_shipping = value.get("freeShipping")
        return {
            "title": value["name"],
            "price": money(value.get("salePrice"), "USD"),
            "product_url": _https_url(value.get("url"), "Best Buy product URL"),
            "image_urls": [_https_url(image, "Best Buy image URL")] if image is not None else [],
            "available": available,
            "item_ref": item_ref(self.platform, {"sku": sku}),
            **({"shipping_options": [{
                "scope": "catalog",
                "free_shipping": free_shipping,
                "shipping_cost": value.get("shippingCost"),
            }]} if isinstance(free_shipping, bool) else {}),
        }

    def quote(self, session: Session, detection: DetectedStore, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        del session, detection, lines, destination
        return api_error(
            self.platform,
            "quote",
            "Best Buy publishes catalog-level freeShipping and shippingCost in product "
            "detail; the API computes no destination shipping quote",
        )


class Etsy:
    platform = "etsy"

    def __init__(self, settings: dict[str, Any]) -> None:
        self.credentials = settings.get("etsy")

    def _headers(self) -> dict[str, str]:
        if not isinstance(self.credentials, dict) or set(self.credentials) != {"keystring", "shared_secret"}:
            raise ToolError("Configure settings.etsy with keystring and shared_secret")
        keystring, secret = self.credentials["keystring"], self.credentials["shared_secret"]
        if not isinstance(keystring, str) or not isinstance(secret, str):
            raise ToolError("Etsy credentials must be strings")
        return {"x-api-key": f"{keystring}:{secret}"}

    def _payload(self, response: httpx.Response, stage: str, context: str) -> dict[str, Any]:
        payload = json_object(response, context)
        if response.status_code == 200:
            return payload
        error = payload.get("error")
        if isinstance(error, str) and "API key" in error:
            raise ToolError(f"Etsy rejected settings.etsy keystring and shared_secret: {error}")
        return api_error(self.platform, stage, f"{context} failed", response.status_code)

    def search(self, session: Session, detection: DetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del detection, destination
        response = session.request(
            "GET",
            ETSY_API + "/listings/active",
            headers=self._headers(),
            params={"keywords": query, "limit": str(limit)},
        )
        payload = self._payload(response, "search", "Etsy active listings")
        if payload.get("status") == "api_error":
            return payload
        results = payload.get("results")
        if not isinstance(results, list):
            raise ToolError("Etsy active listings omitted their results")
        return {"kind": "search", "platform": self.platform, "items": [self._item(value, []) for value in results]}

    def product(self, session: Session, detection: DetectedStore, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del detection, destination
        reference = item.get("ref")
        source = reference.get("listing_id") if isinstance(reference, dict) else item.get("url")
        if not isinstance(source, (str, int)):
            raise ToolError("Etsy product requires a listing_id ref or listing URL")
        listing_id = etsy_listing_id(str(source))
        headers = self._headers()
        listing = self._payload(
            session.request("GET", f"{ETSY_API}/listings/{listing_id}", headers=headers),
            "product",
            "Etsy listing",
        )
        if listing.get("status") == "api_error":
            return listing
        images = self._payload(
            session.request("GET", f"{ETSY_API}/listings/{listing_id}/images", headers=headers),
            "product",
            "Etsy listing images",
        )
        if images.get("status") == "api_error":
            return images
        results = images.get("results")
        if not isinstance(results, list):
            raise ToolError("Etsy listing images omitted their results")
        return self._item(listing, [
            _https_url(image.get("url_fullxfull"), "Etsy image URL")
            for image in results
            if isinstance(image, dict)
        ])

    def _item(self, value: object, images: list[str]) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("title"), str) or not value["title"]:
            raise ToolError("Etsy listing requires a title")
        listing_id = str(value.get("listing_id", ""))
        quantity = value.get("quantity")
        variations = value.get("has_variations")
        if not listing_id.isdecimal() or isinstance(quantity, bool) or not isinstance(quantity, int) or not isinstance(variations, bool):
            raise ToolError("Etsy listing omitted listing_id, quantity, or has_variations")
        return {
            "title": value["title"],
            "description": value.get("description", ""),
            "price": _etsy_money(value.get("price")),
            "product_url": _https_url(value.get("url"), "Etsy listing URL"),
            "image_urls": images,
            "available": value.get("state") == "active" and quantity > 0,
            "item_ref": item_ref(self.platform, {"listing_id": listing_id}),
            # A listing with variations prices its cheapest offering; exact
            # per-variation prices need OAuth the anonymous key cannot mint.
            **({"variant": "Lowest-priced variation"} if variations else {}),
        }

    def quote(self, session: Session, detection: DetectedStore, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        del detection
        quantities: dict[str, int] = {}
        for line in lines:
            listing_id = str(parse_item_ref(line["ref"], self.platform)["listing_id"])
            quantities[listing_id] = quantities.get(listing_id, 0) + line["quantity"]
        response = session.request(
            "GET",
            ETSY_API + "/listings/batch",
            headers=self._headers(),
            params={
                "listing_ids": ",".join(quantities),
                "includes": "BuyerPrice",
                "buyer_country": destination["country"],
            },
        )
        payload = self._payload(response, "quote", "Etsy buyer-price batch")
        if payload.get("status") == "api_error":
            return payload
        results = payload.get("results")
        if not isinstance(results, list) or len(results) != len(quantities):
            raise ToolError("Etsy buyer-price batch did not return every requested listing")
        subtotal, shipping, currencies = Decimal(0), Decimal(0), set()
        undeliverable = False
        for value in results:
            if not isinstance(value, dict):
                raise ToolError("Etsy buyer-price result must be an object")
            listing_id = str(value.get("listing_id", ""))
            price = value.get("buyer_price")
            if listing_id not in quantities or not isinstance(price, dict):
                raise ToolError("Etsy buyer-price result has no requested listing or buyer_price")
            free = price.get("is_free_shipping")
            if not isinstance(free, bool):
                raise ToolError("Etsy buyer price omitted is_free_shipping")
            base = _etsy_money(price.get("base_price"))
            currencies.add(base["currency"])
            subtotal += Decimal(base["amount"]) * quantities[listing_id]
            cost = price.get("shipping_cost")
            if cost is not None:
                line_shipping = _etsy_money(cost)
                currencies.add(line_shipping["currency"])
                shipping += Decimal(line_shipping["amount"])
            elif not free:
                undeliverable = True
        if len(currencies) != 1:
            raise ToolError("Etsy buyer prices returned mixed currencies")
        currency = currencies.pop()
        context = EtsyQuote(delivery_scope="destination_country")
        country = destination["country"]
        options = [
            shipping_option(
                context,
                "etsy-buyer-price",
                f"Etsy buyer-price shipping to {country}, one shipment per listing",
                "unavailable" if undeliverable else "delivery",
                None if undeliverable else money(shipping, currency),
            )
        ]
        return quote_outcome(
            context,
            options,
            money(subtotal, currency),
            no_quote_reason=f"no_etsy_shipping_to_{country}",
        )


def _etsy_money(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ToolError("Etsy money must be an object")
    amount, divisor, currency = value.get("amount"), value.get("divisor"), value.get("currency_code")
    digits = len(str(divisor)) - 1 if type(divisor) is int and divisor > 0 else -1
    if (
        isinstance(amount, bool)
        or not isinstance(amount, int)
        or digits < 0
        or 10**digits != divisor
        or not isinstance(currency, str)
    ):
        raise ToolError(
            "Etsy money requires an integer amount, a power-of-ten divisor, and a currency"
        )
    return minor_money(str(amount), currency, digits)


class Ebay:
    platform = "ebay"

    def __init__(self, settings: dict[str, Any]) -> None:
        self.credentials = settings.get("ebay")
        self.token: str | None = None

    def _headers(self, session: Session, destination: dict[str, str]) -> dict[str, str] | dict[str, Any]:
        if not isinstance(self.credentials, dict) or set(self.credentials) != {"client_id", "client_secret"}:
            raise ToolError("Configure settings.ebay with client_id and client_secret")
        client_id, secret = self.credentials["client_id"], self.credentials["client_secret"]
        if not isinstance(client_id, str) or not isinstance(secret, str):
            raise ToolError("eBay credentials must be strings")
        if self.token is None:
            basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
            response = session.request("POST", EBAY_TOKEN, headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"}, data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"})
            payload = json_object(response, "eBay OAuth")
            if response.status_code != 200 or not isinstance(payload.get("access_token"), str):
                raise ToolError("eBay OAuth client-credentials token request failed")
            self.token = payload["access_token"]
        location = quote(
            f"country={destination['country']},zip={destination['postal_code']}",
            safe=",",
        )
        return {
            "Authorization": f"Bearer {self.token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            "X-EBAY-C-ENDUSERCTX": f"contextualLocation={location}",
        }

    def search(self, session: Session, detection: DetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del detection
        response = session.request("GET", EBAY_API + "/buy/browse/v1/item_summary/search", headers=self._headers(session, destination), params={"q": query, "limit": str(limit)})
        payload = json_object(response, "eBay Browse search")
        if response.status_code != 200:
            return api_error(self.platform, "search", "eBay Browse search failed", response.status_code)
        values = payload.get("itemSummaries", [])
        if not isinstance(values, list):
            raise ToolError("eBay Browse search omitted itemSummaries")
        return {"kind": "search", "platform": self.platform, "items": [self._item(value) for value in values]}

    def _item(self, value: object) -> dict[str, Any]:
        if not isinstance(value, dict) or not isinstance(value.get("itemId"), str) or not isinstance(value.get("title"), str):
            raise ToolError("eBay item summary has invalid identity")
        price = value.get("price")
        if not isinstance(price, dict) or not isinstance(value.get("itemWebUrl"), str):
            raise ToolError("eBay item summary omitted price or URL")
        image = value.get("image")
        return {"title": value["title"], "price": money(price.get("value"), price.get("currency")), "product_url": value["itemWebUrl"], "image_urls": [image["imageUrl"]] if isinstance(image, dict) and isinstance(image.get("imageUrl"), str) else [], "item_ref": item_ref(self.platform, {"item_id": value["itemId"]})}

    def product(self, session: Session, detection: DetectedStore, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del detection
        reference = item.get("ref")
        item_id = reference.get("item_id") if isinstance(reference, dict) else item.get("item_id")
        if isinstance(item_id, str):
            response = session.request(
                "GET",
                EBAY_API + "/buy/browse/v1/item/" + quote(item_id, safe=""),
                headers=self._headers(session, destination),
            )
        else:
            url = item.get("url")
            if not isinstance(url, str):
                raise ToolError("eBay product requires an item URL or ref")
            legacy_id = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
            if not legacy_id.isdecimal():
                raise ToolError("eBay item URL must end with a numeric legacy item ID")
            response = session.request(
                "GET",
                EBAY_API + "/buy/browse/v1/item/get_item_by_legacy_id",
                headers=self._headers(session, destination),
                params={"legacy_item_id": legacy_id},
            )
        payload = json_object(response, "eBay Browse item")
        if response.status_code != 200:
            return api_error(self.platform, "product", "eBay Browse getItem failed", response.status_code)
        detail = self._item({**payload, "itemWebUrl": payload.get("itemWebUrl", item.get("url"))})
        detail["description"] = payload.get("shortDescription", "")
        shipping = payload.get("shippingOptions")
        if isinstance(shipping, list) and shipping:
            detail["shipping_options"] = shipping
        return detail

    def quote(self, session: Session, detection: DetectedStore, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        del session, detection, lines, destination
        return api_error(self.platform, "quote", "eBay shipping appears in product detail; checkout APIs are restricted-tier")


class ShopifyGlobal:
    platform = "shopify_global"

    def __init__(self, settings: dict[str, Any]) -> None:
        value = settings.get("shopify_global")
        self.profile_url = value.get("profile_url") if isinstance(value, dict) else None

    def _call(self, session: Session, name: str, catalog: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(self.profile_url, str):
            raise ToolError("Configure settings.shopify_global.profile_url with a public UCP agent profile")
        arguments = {
            "meta": {"ucp-agent": {"profile": self.profile_url}},
            "catalog": catalog,
        }
        response = session.request(
            "POST",
            SHOPIFY_GLOBAL_MCP,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        payload = json_object(response, "Shopify Global Catalog MCP")
        if response.status_code != 200 or "error" in payload:
            return api_error(self.platform, name, "Shopify Global Catalog request failed", response.status_code)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ToolError("Shopify Global Catalog omitted result")
        structured = result.get("structuredContent", result)
        if not isinstance(structured, dict):
            raise ToolError("Shopify Global Catalog result must be an object")
        ucp = structured.get("ucp")
        if isinstance(ucp, dict) and ucp.get("status") == "error":
            messages = structured.get("messages", [])
            reason = "Shopify Global Catalog returned a UCP application error"
            if isinstance(messages, list):
                values = [
                    message.get("content")
                    for message in messages
                    if isinstance(message, dict)
                    and isinstance(message.get("content"), str)
                ]
                if values:
                    reason += ": " + "; ".join(values)
            return api_error(self.platform, name, reason)
        return structured

    def search(self, session: Session, detection: DetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del detection
        payload = self._call(
            session,
            "search_catalog",
            {
                "query": query,
                "filters": {"ships_to": _ships_to(destination)},
                "pagination": {"limit": limit},
                "view": "offer",
            },
        )
        if payload.get("status") == "api_error":
            return payload
        products = payload.get("products", [])
        if not isinstance(products, list):
            raise ToolError("Shopify Global Catalog omitted products")
        items = [item for product in products for item in self._items(product)]
        return {"kind": "search", "platform": self.platform, "items": items}

    def _items(self, value: object) -> list[dict[str, Any]]:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("id"), str)
            or not isinstance(value.get("title"), str)
        ):
            raise ToolError("Shopify Global Catalog product has invalid identity")
        product_id = value["id"]
        if not product_id.startswith("gid://shopify/p/"):
            raise ToolError("Shopify Global Catalog product has invalid universal product ID")
        description = value.get("description", {})
        if not isinstance(description, dict):
            raise ToolError("Shopify Global Catalog description must be an object")
        text = description.get("html", description.get("plain", ""))
        if not isinstance(text, str):
            raise ToolError("Shopify Global Catalog description text must be a string")
        media = value.get("media", [])
        variants = value.get("variants", [])
        if not isinstance(media, list) or not isinstance(variants, list):
            raise ToolError("Shopify Global Catalog media and variants must be arrays")
        images = [
            item["url"]
            for item in media
            if isinstance(item, dict)
            and item.get("type") == "image"
            and isinstance(item.get("url"), str)
        ]
        product_url = value.get("url")
        if not isinstance(product_url, str):
            raise ToolError("Shopify Global Catalog product omitted its merchant URL")
        items = []
        for variant in variants:
            if (
                not isinstance(variant, dict)
                or not isinstance(variant.get("id"), str)
                or not isinstance(variant.get("title"), str)
            ):
                raise ToolError("Shopify Global Catalog variant has invalid identity")
            seller = variant.get("seller")
            if (
                not isinstance(seller, dict)
                or not isinstance(seller.get("url"), str)
                or not isinstance(seller.get("domain"), str)
            ):
                raise ToolError(
                    "Shopify Global Catalog variant omitted seller URL or domain"
                )
            seller_url = seller["url"]
            api_origin = "https://" + seller["domain"]
            variant_url = variant.get("url")
            checkout_url = variant.get("checkout_url")
            if variant_url is not None and not isinstance(variant_url, str):
                raise ToolError("Shopify Global Catalog variant URL must be a string")
            if checkout_url is not None and not isinstance(checkout_url, str):
                raise ToolError(
                    "Shopify Global Catalog checkout URL must be a string"
                )
            availability = variant.get("availability")
            if not isinstance(availability, dict) or not isinstance(availability.get("available"), bool):
                raise ToolError("Shopify Global Catalog variant omitted availability")
            options = variant.get("options", [])
            if not isinstance(options, list):
                raise ToolError("Shopify Global Catalog variant options must be an array")
            items.append({
                "title": value["title"], "description": text,
                "price": _ucp_money(variant.get("price")),
                "product_url": variant_url or seller_url,
                "image_urls": images, "available": availability["available"],
                "variant": variant["title"], "options": options,
                "item_ref": item_ref(self.platform, {"product_id": product_id, "variant_id": variant["id"]}),
                "seller_url": seller_url,
                "seller_domain": seller["domain"],
                "variant_url": variant_url,
                "checkout_url": checkout_url,
                "merchant_stores": [{"url": seller_url, "api_origin": api_origin}],
            })
        if items:
            return items
        price_range = value.get("price_range")
        if not isinstance(price_range, dict):
            raise ToolError("Shopify Global Catalog product omitted price_range")
        return [{
            "title": value["title"], "description": text,
            "price": _ucp_money(price_range.get("min")),
            "max_price": _ucp_money(price_range.get("max", price_range.get("min"))),
            "product_url": product_url, "image_urls": images,
            "item_ref": item_ref(self.platform, {"product_id": product_id}),
            "merchant_stores": [{"url": product_url, "api_origin": url_origin(product_url)}],
        }]

    def product(self, session: Session, detection: DetectedStore, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del detection
        reference = item.get("ref")
        product_id = reference.get("product_id") if isinstance(reference, dict) else item.get("product_id")
        if not isinstance(product_id, str):
            raise ToolError("Shopify Global product requires a product ref")
        payload = self._call(
            session,
            "get_product",
            {
                "id": product_id,
                "filters": {"ships_to": _ships_to(destination)},
                "view": "summary",
            },
        )
        if payload.get("status") == "api_error":
            return payload
        return {"kind": "search", "platform": self.platform, "items": self._items(payload.get("product"))}

    def quote(self, session: Session, detection: DetectedStore, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        del session, detection, lines, destination
        return api_error(self.platform, "quote", "Quote a specific merchant storefront from the catalog result")
