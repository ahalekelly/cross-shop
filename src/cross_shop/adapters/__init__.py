"""Storefront and marketplace adapters used by cross-shop.

Each adapter exposes search, product, and quote operations. Operations return an
`api_error` envelope or items accepted by `core.normalize_variant`. Storefront
modules also expose `detect()` for platform discovery.
"""
