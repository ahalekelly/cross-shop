from __future__ import annotations

from functools import cache
from typing import Any

from mcp.server.fastmcp import FastMCP

from cross_shop.service import CrossShop

mcp = FastMCP("cross-shop")


@cache
def _service() -> CrossShop:
    return CrossShop()


@mcp.tool(
    description=(
        "Search 1–100 {store, query} entries. Results use r<run>.<s>.<i> item "
        "handles; append .<v> for a variant, then pass handles to product or quote."
    )
)
def search(
    entries: list[dict[str, str]],
    limit: int = 20,
    description_chars: int = 300,
    redetect: bool = False,
) -> dict[str, Any]:
    return _service().search(
        entries,
        limit=limit,
        description_chars=description_chars,
        redetect=redetect,
    )


@mcp.tool(
    description=(
        "Fetch detail for 1–100 product URLs, refs, or "
        "r<run>.<s>.<i>[.<v>] handles. Product results issue fresh handles for quote."
    )
)
def product(
    inputs: list[str | dict[str, Any]],
    description_chars: int = 2000,
    redetect: bool = False,
) -> dict[str, Any]:
    return _service().product(
        inputs,
        description_chars=description_chars,
        redetect=redetect,
    )


@mcp.tool(
    description=(
        "Quote 1–20 stores with 1–20 lines each. Each item accepts a URL, ref, or "
        "r<run>.<s>.<i>[.<v>] handle; use .<v> for multi-variant products."
    )
)
def quote(
    quotes: list[dict[str, Any]],
    destination: dict[str, str] | None = None,
    redetect: bool = False,
) -> dict[str, Any]:
    return _service().quote(
        quotes,
        destination=destination,
        redetect=redetect,
    )


@mcp.tool(
    description=(
        "Download up to ten cached images for item handle r<run>.<s>.<i>; "
        "selection is N or START:END."
    )
)
def images(handle: str, selection: str | None = None) -> list[str]:
    return _service().images_for(handle, selection)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
