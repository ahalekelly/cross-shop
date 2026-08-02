"""Storefront platform adapters for the product-search skill.

Every adapter is a class exposing the same three operations, each taking the
store worker's `Session` and its `PositiveDetection`, and returning either an
`api_error` envelope or a result whose items `core.normalize_variant` accepts:

    search(session, detection, query, limit, destination) -> dict
    product(session, detection, item, destination) -> dict
    quote(session, detection, lines, destination) -> dict

`item` is the caller's resolved product input: `{"store", "ref"?, "url"?,
"cached"?}`, where `ref` is a durable ref object and `url` a canonical product
page URL. `lines` is a list of `{"ref", "quantity", "cached", "url"}`, and one
`quote` call builds one cart holding every line.

An adapter owns everything platform-specific about its storefront: how a ref or
URL resolves to live detail, which requests are signed, and how many lines its
cart accepts. Modules also expose `detect()`, which the service calls before
any adapter is chosen.
"""
