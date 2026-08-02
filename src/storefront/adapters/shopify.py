from __future__ import annotations

import json
import re
from email import policy
from email.parser import BytesParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from storefront.core import (
    DetectedStore,
    Http,
    ShopifyQuote,
    ShopifySearch,
    ShopifyShipping,
    ToolError,
    bot_wall,
    destination_for,
    gated,
    item_ref,
    json_object,
    money,
    quote_outcome,
    search_result,
    shipping_option,
    wall_system,
)

API_PATH = "/api/2026-07/graphql.json"
PRODUCT_QUERY = """
query ProductSearch($query: String!) {
  products(first: 20, query: $query) {
    nodes {
      title handle vendor productType description
      images(first: 10) { nodes { url } }
      variants(first: 50) {
        nodes {
          id title sku barcode availableForSale weight weightUnit
          selectedOptions { name value }
          price { amount currencyCode }
          compareAtPrice { amount currencyCode }
        }
      }
    }
  }
}
"""
PRODUCT_DETAIL_QUERY = """
query ProductDetail($id: ID!) {
  node(id: $id) {
    ... on ProductVariant {
      product {
        title handle description
        images(first: 10) { nodes { url } }
        variants(first: 50) {
          nodes {
            id title sku barcode availableForSale weight weightUnit
            selectedOptions { name value }
            price { amount currencyCode }
            compareAtPrice { amount currencyCode }
          }
        }
      }
    }
  }
}
"""
CART_CREATE_MUTATION = """
mutation CartCreate($input: CartInput!) {
  cartCreate(input: $input) {
    cart { id cost { subtotalAmount { amount currencyCode } } }
    userErrors { field code message }
  }
}
"""
RATE_QUERY = """
query CartRates($id: ID!) {
  cart(id: $id) {
    id
    ... @defer {
      deliveryGroups(first: 10, withCarrierRates: true) {
        nodes {
          groupType
          deliveryOptions {
            handle title code description deliveryMethodType
            estimatedCost { amount currencyCode }
          }
        }
      }
    }
  }
}
"""
class SignedRedirectBoundary(ToolError):
    """A signed request cannot safely follow the storefront redirect."""


def detect(
    http: Http, origin: str, entry_url: str, homepage: httpx.Response
) -> DetectedStore | None:
    candidates = [origin]
    backends = sorted(
        set(
            re.findall(
                r"(?<![a-z0-9.-])([a-z0-9][a-z0-9-]*\.myshopify\.com)(?![a-z0-9.-])",
                homepage.text,
                re.IGNORECASE,
            )
        )
    )
    if len(backends) > 1:
        raise ToolError(
            "Shopify detection found multiple .myshopify.com API candidates"
        )
    if backends:
        backend = "https://" + backends[0].lower()
        if backend != origin:
            candidates.append(backend)
    for api_origin in candidates:
        try:
            response = _signed_post(
                http,
                api_origin + API_PATH,
                {"query": "{ shop { name } }"},
                "application/json",
            )
        except SignedRedirectBoundary:
            continue
        if response.status_code != 200:
            continue
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("data"), dict)
            and isinstance(payload["data"].get("shop"), dict)
            and isinstance(payload["data"]["shop"].get("name"), str)
        ):
            evidence = "tokenless Storefront GraphQL data.shop"
            if api_origin != origin:
                evidence += f" via {api_origin}"
            return DetectedStore(
                origin=origin,
                entry_url=entry_url,
                platform="shopify",
                api_origin=api_origin,
                evidence=(evidence,),
            )
    return None


def search(http: Http, detection: DetectedStore, query: str) -> dict[str, object]:
    response = _graphql(http, detection, PRODUCT_QUERY, {"query": query})
    terminal = _terminal_response("search", response)
    if terminal is not None:
        return terminal
    payload = json_object(response, "Shopify product search")
    data = _graphql_data(payload, "Shopify product search")
    products = data.get("products")
    if not isinstance(products, dict) or not isinstance(products.get("nodes"), list):
        raise ToolError("Shopify product search response has no product nodes")

    items: list[dict[str, Any]] = []
    for product in products["nodes"]:
        if not isinstance(product, dict):
            raise ToolError("Shopify product node must be an object")
        variants = product.get("variants")
        if not isinstance(variants, dict) or not isinstance(
            variants.get("nodes"), list
        ):
            raise ToolError("Shopify product has no variant nodes")
        for variant in variants["nodes"]:
            items.append(_product_item(detection, product, variant))
    return search_result(ShopifySearch(), query, items)


def product_detail(
    http: Http, detection: DetectedStore, reference: object
) -> dict[str, object]:
    from storefront.core import parse_item_ref

    selected = parse_item_ref(reference, "shopify")
    variant_id = selected.get("variant_id")
    if not isinstance(variant_id, str):
        raise ToolError("Shopify ref has no variant ID")
    response = _graphql(http, detection, PRODUCT_DETAIL_QUERY, {"id": variant_id})
    terminal = _terminal_response("product", response)
    if terminal is not None:
        return terminal
    data = _graphql_data(json_object(response, "Shopify product detail"), "Shopify product detail")
    node = data.get("node")
    product = node.get("product") if isinstance(node, dict) else None
    variants = product.get("variants") if isinstance(product, dict) else None
    nodes = variants.get("nodes") if isinstance(variants, dict) else None
    if not isinstance(product, dict) or not isinstance(nodes, list):
        raise ToolError("Shopify ref no longer resolves to a product variant")
    return search_result(
        ShopifySearch(),
        variant_id,
        [_product_item(detection, product, value) for value in nodes],
    )


def quote(http: Http, detection: DetectedStore, reference: object) -> dict[str, object]:
    from storefront.core import DEFAULT_DESTINATION

    return quote_many(
        http,
        detection,
        [{"ref": reference, "quantity": 1}],
        DEFAULT_DESTINATION,
    )


def quote_many(
    http: Http,
    detection: DetectedStore,
    lines: list[dict[str, Any]],
    destination: dict[str, str],
) -> dict[str, object]:
    from storefront.core import parse_item_ref

    cart_lines = []
    for line in lines:
        selected = parse_item_ref(line["ref"], "shopify")
        variant_id = selected.get("variant_id")
        if not isinstance(variant_id, str) or not variant_id.startswith(
            "gid://shopify/ProductVariant/"
        ):
            raise ToolError("Shopify ref has no valid variant ID")
        cart_lines.append(
            {"merchandiseId": variant_id, "quantity": line["quantity"]}
        )
    address = destination_for("shopify", destination)
    create_response = _graphql(
        http,
        detection,
        CART_CREATE_MUTATION,
        {
            "input": {
                "lines": cart_lines,
                "buyerIdentity": {
                    "countryCode": destination["country"],
                    "deliveryAddressPreferences": [{"deliveryAddress": address}],
                },
            }
        },
    )
    terminal = _terminal_response("quote", create_response)
    if terminal is not None:
        return terminal
    create_data = _graphql_data(
        json_object(create_response, "Shopify cart creation"),
        "Shopify cart creation",
    )
    cart_create = create_data.get("cartCreate")
    if not isinstance(cart_create, dict):
        raise ToolError("Shopify cart creation response has no cartCreate object")
    user_errors = cart_create.get("userErrors")
    if not isinstance(user_errors, list):
        raise ToolError("Shopify cart creation response has no userErrors array")
    if user_errors:
        raise ToolError(f"Shopify rejected cart creation: {_messages(user_errors)}")
    cart = cart_create.get("cart")
    if not isinstance(cart, dict) or not isinstance(cart.get("id"), str):
        raise ToolError("Shopify cart creation response has no cart ID")
    subtotal = _shopify_money(cart.get("cost"), "subtotalAmount", "Shopify cart subtotal")
    rates_response = _graphql(
        http,
        detection,
        RATE_QUERY,
        {"id": cart["id"]},
        accept="multipart/mixed; deferSpec=20220824, application/json",
    )
    terminal = _terminal_response("quote", rates_response)
    if terminal is not None:
        return terminal
    options = [
        _shipping_option(option)
        for group in _delivery_groups(rates_response)
        for option in _required_list(group, "deliveryOptions", "Shopify delivery group")
    ]
    return quote_outcome(ShopifyQuote(), options, subtotal)


def _graphql(
    http: Http,
    detection: DetectedStore,
    query: str,
    variables: dict[str, Any],
    *,
    accept: str = "application/json",
) -> httpx.Response:
    if detection.platform != "shopify":
        raise ToolError("Shopify adapter requires a detected Shopify API origin")
    target = detection.api_origin + API_PATH
    payload = {"query": query, "variables": variables}
    return _signed_post(http, target, payload, accept)


def _signed_post(
    http: Http, target: str, payload: dict[str, Any], accept: str
) -> httpx.Response:
    api = urlsplit(target)
    api_authority = (api.hostname, api.port or 443)
    for attempt in range(2):
        request = http.client.build_request(
            "POST",
            target,
            headers={"Content-Type": "application/json", "Accept": accept},
            json=payload,
        )
        response = http.send_shopify(request)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        if attempt == 1:
            raise SignedRedirectBoundary("Shopify GraphQL exceeded one signed redirect")
        location = response.headers.get("location")
        if not location:
            raise SignedRedirectBoundary(
                "Shopify GraphQL redirect has no Location header"
            )
        target = urljoin(target, location)
        parts = urlsplit(target)
        try:
            redirect_authority = (parts.hostname, parts.port or 443)
        except ValueError as error:
            raise SignedRedirectBoundary(
                "Shopify GraphQL redirect target has an invalid port"
            ) from error
        if parts.scheme != "https" or redirect_authority != api_authority:
            raise SignedRedirectBoundary(
                "Shopify GraphQL redirect target must use the detected API authority"
            )
        if parts.username is not None or parts.password is not None:
            raise SignedRedirectBoundary(
                "Shopify GraphQL redirect target must not contain credentials"
            )
        if parts.path != API_PATH or parts.query or parts.fragment:
            raise SignedRedirectBoundary(
                "Shopify GraphQL redirect target must be the Storefront API path"
            )
    raise AssertionError("Shopify redirect loop must return or raise")


def _terminal_response(
    operation: str, response: httpx.Response
) -> dict[str, object] | None:
    system = wall_system(response)
    if system is not None:
        return bot_wall(operation, "shopify", response, system)
    if response.status_code in {401, 403}:
        return gated(
            operation,
            "shopify",
            str(response.url),
            response,
            "Storefront API authorization required",
        )
    if response.status_code >= 400:
        raise ToolError(f"Shopify {operation} returned HTTP {response.status_code}")
    return None


def _graphql_data(payload: dict[str, Any], context: str) -> dict[str, Any]:
    if "errors" in payload:
        raise ToolError(
            f"{context} returned GraphQL errors: {_messages(payload['errors'])}"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ToolError(f"{context} response has no data object")
    return data


def _product_item(
    detection: DetectedStore, product: dict[str, Any], variant: Any
) -> dict[str, Any]:
    if not isinstance(variant, dict) or not isinstance(variant.get("id"), str):
        raise ToolError("Shopify variant must contain an ID")
    price = variant.get("price")
    if not isinstance(price, dict):
        raise ToolError("Shopify variant must contain a price")
    handle = product.get("handle")
    if not isinstance(handle, str) or not handle:
        raise ToolError("Shopify product must contain a handle")
    images = product.get("images")
    image_nodes = images.get("nodes") if isinstance(images, dict) else []
    item = {
        "name": product.get("title"),
        "description": product.get("description"),
        "image_urls": [node["url"] for node in image_nodes if isinstance(node, dict) and isinstance(node.get("url"), str)],
        "options": variant.get("selectedOptions", []),
        "variant": variant.get("title"),
        "sku": variant.get("sku"),
        "barcode": variant.get("barcode"),
        "available": variant.get("availableForSale"),
        "price": money(price.get("amount"), price.get("currencyCode")),
        "product_url": f"{detection.origin}/products/{handle}",
        "item_ref": item_ref("shopify", {"variant_id": variant["id"]}),
    }
    compare_at = variant.get("compareAtPrice")
    if compare_at is not None:
        if not isinstance(compare_at, dict):
            raise ToolError("Shopify compare-at price must be an object or null")
        item["compare_at_price"] = money(
            compare_at.get("amount"), compare_at.get("currencyCode")
        )
    if variant.get("weight") is not None:
        item["weight"] = {"value": variant["weight"], "unit": variant.get("weightUnit")}
    return item


def _delivery_groups(response: httpx.Response) -> list[dict[str, Any]]:
    content_type = response.headers.get("content-type", "")
    payloads = (
        _multipart_payloads(response)
        if content_type.lower().startswith("multipart/mixed")
        else [json_object(response, "Shopify rates")]
    )
    groups: list[dict[str, Any]] | None = None
    terminal_seen = len(payloads) == 1
    for payload in payloads:
        if "errors" in payload:
            raise ToolError(
                f"Shopify rates returned GraphQL errors: {_messages(payload['errors'])}"
            )
        if payload.get("hasNext") is False:
            terminal_seen = True
        data = payload.get("data")
        if isinstance(data, dict):
            cart = data.get("cart")
            if cart is None:
                raise ToolError("Shopify rates response has no cart")
            if isinstance(cart, dict) and "deliveryGroups" in cart:
                groups = _group_nodes(cart["deliveryGroups"])
        incremental = payload.get("incremental", [])
        if not isinstance(incremental, list):
            raise ToolError("Shopify incremental payload must be an array")
        for patch in incremental:
            if (
                not isinstance(patch, dict)
                or patch.get("path") != ["cart"]
                or not isinstance(patch.get("data"), dict)
            ):
                raise ToolError(
                    "Shopify rates returned an unexpected incremental patch"
                )
            if "deliveryGroups" in patch["data"]:
                groups = _group_nodes(patch["data"]["deliveryGroups"])
    if not terminal_seen:
        raise ToolError(
            "Shopify multipart rates response has no terminal hasNext=false part"
        )
    if groups is None:
        raise ToolError("Shopify rates response has no delivery groups")
    return groups


def _multipart_payloads(response: httpx.Response) -> list[dict[str, Any]]:
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + response.headers["content-type"].encode()
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + response.content
    )
    if not message.is_multipart():
        raise ToolError("Shopify multipart response has no valid boundary")
    payloads: list[dict[str, Any]] = []
    for part in message.iter_parts():
        raw = part.get_payload(decode=True)
        if raw is None:
            raise ToolError("Shopify multipart part has no body")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ToolError("Shopify multipart part is not JSON") from error
        if not isinstance(payload, dict):
            raise ToolError("Shopify multipart JSON part must be an object")
        payloads.append(payload)
    if not payloads:
        raise ToolError("Shopify multipart response has no parts")
    return payloads


def _group_nodes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        raise ToolError("Shopify deliveryGroups must be an object")
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
        raise ToolError("Shopify deliveryGroups must contain object nodes")
    return nodes


def _shipping_option(option: Any) -> dict[str, Any]:
    if not isinstance(option, dict):
        raise ToolError("Shopify delivery option must be an object")
    title = option.get("title")
    if not isinstance(title, str) or not title:
        raise ToolError("Shopify delivery option has no title")
    estimated = option.get("estimatedCost")
    amount = (
        None
        if estimated is None
        else _shopify_money({"value": estimated}, "value", "Shopify delivery option")
    )
    method = option.get("deliveryMethodType")
    if not isinstance(method, str) or method not in {
        "LOCAL",
        "NONE",
        "PICK_UP",
        "PICKUP_POINT",
        "RETAIL",
        "SHIPPING",
    }:
        raise ToolError("Shopify delivery option has invalid deliveryMethodType")
    if method in {"PICK_UP", "PICKUP_POINT", "RETAIL"}:
        disposition = "pickup"
    elif method == "NONE":
        disposition = "unavailable"
    elif amount is None:
        disposition = "paid_later"
    else:
        disposition = "delivery"
    option_id = next(
        (
            value
            for value in (option.get("handle"), option.get("code"))
            if isinstance(value, str) and value
        ),
        title,
    )
    return shipping_option(
        ShopifyShipping(code=option.get("code"), description=option.get("description")),
        option_id,
        title,
        disposition,
        amount,
    )


def _shopify_money(container: Any, key: str, context: str) -> dict[str, str]:
    if not isinstance(container, dict) or not isinstance(container.get(key), dict):
        raise ToolError(f"{context} is missing")
    value = container[key]
    return money(value.get("amount"), value.get("currencyCode"))


def _required_list(value: dict[str, Any], name: str, context: str) -> list[Any]:
    result = value.get(name)
    if not isinstance(result, list):
        raise ToolError(f"{context} has no {name} array")
    return result


def _messages(errors: Any) -> str:
    if (
        not isinstance(errors, list)
        or not errors
        or any(
            not isinstance(error, dict)
            or not isinstance(error.get("message"), str)
            or not error["message"]
            for error in errors
        )
    ):
        raise ToolError(
            "Shopify GraphQL errors must be a nonempty array of objects with nonempty messages"
        )
    return "; ".join(error["message"] for error in errors)
