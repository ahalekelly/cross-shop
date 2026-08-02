from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
from typing import Any
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from storefront.core import (
    DetectedStore,
    Session,
    ToolError,
    api_error,
    item_ref,
    json_object,
    minor_money,
    money,
    url_origin,
)

ALIEXPRESS_URL = "https://api.taobao.com/router/rest"
SERPAPI_URL = "https://serpapi.com/search.json"
EBAY_API = "https://api.ebay.com"
EBAY_TOKEN = "https://api.ebay.com/identity/v1/oauth2/token"
SHOPIFY_GLOBAL_MCP = "https://catalog.shopify.com/api/ucp/mcp"
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
        self.engine = engine
        self.platform = "google_shopping" if engine == "google_shopping" else "amazon"

    def search(self, session: Session, detection: DetectedStore, query: str, limit: int, destination: dict[str, str]) -> dict[str, Any]:
        del detection, destination
        key = os.environ.get("SERPAPI_API_KEY")
        if not key:
            return api_error(self.platform, "search", "Set SERPAPI_API_KEY")
        params = {"engine": self.engine, "api_key": key}
        params |= {"q": query, "location": "San Francisco, California, United States", "gl": "us", "hl": "en", "direct_link": "true"} if self.engine == "google_shopping" else {"k": query, "amazon_domain": "amazon.com", "language": "en_US"}
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
            reference = item_ref(self.platform, {"asin": asin, "merchant_url": url})
        else:
            product_id = str(value.get("product_id", value.get("position", "")))
            if not product_id:
                raise ToolError("SerpApi Shopping result requires product_id or position")
            reference = item_ref(self.platform, {"product_id": product_id, "merchant_url": url})
        thumbnail = value.get("thumbnail")
        images = [_https_url(thumbnail, "SerpApi thumbnail")] if thumbnail is not None else []
        return {"title": value["title"], "price": money(price, "USD"), "product_url": url, "image_urls": images, "item_ref": reference, "lead": True}

    def product(self, session: Session, detection: DetectedStore, item: dict[str, Any], destination: dict[str, str]) -> dict[str, Any]:
        del session, detection, destination
        reason = "Google Shopping results are leads; open the merchant link" if self.engine == "google_shopping" else "No anonymous Amazon product API exists"
        return {**api_error(self.platform, "product", reason), **({"merchant_url": item["url"]} if isinstance(item.get("url"), str) else {})}

    def quote(self, session: Session, detection: DetectedStore, lines: list[dict[str, Any]], destination: dict[str, str]) -> dict[str, Any]:
        del session, detection, lines, destination
        reason = "Quote the merchant storefront from the Google Shopping lead" if self.engine == "google_shopping" else "No anonymous Amazon cart API exists"
        return api_error(self.platform, "quote", reason)


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
