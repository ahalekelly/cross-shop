from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from storefront.core import ToolError
from storefront.service import Storefront
from storefront.storage import DataStore


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        data = DataStore()
        if args.command == "config":
            result = _config(data, args)
        else:
            tool = Storefront(data)
            if args.command == "search":
                result = tool.search(_json(args.entries, "search entries"), limit=args.limit, description_chars=args.description_chars, redetect=args.redetect, debug=args.debug)
            elif args.command == "product":
                result = tool.product(_json(args.items, "product items"), description_chars=args.description_chars, redetect=args.redetect, debug=args.debug)
            elif args.command == "quote":
                destination = _json(args.destination, "destination") if args.destination is not None else None
                result = tool.quote(_json(args.quotes, "quotes"), destination=destination, redetect=args.redetect, debug=args.debug)
            elif args.command == "images":
                result = tool.images_for(args.handle, args.range)
            else:
                raise AssertionError(args.command)
    except ToolError as error:
        print(f"tool_error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return int(_has_api_error(result))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search and quote public storefronts in token-lean batches")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search")
    search.add_argument("entries")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--description-chars", type=int, default=300)
    _common(search)
    product = commands.add_parser("product")
    product.add_argument("items")
    product.add_argument("--description-chars", type=int, default=2000)
    _common(product)
    quote = commands.add_parser("quote")
    quote.add_argument("quotes")
    quote.add_argument("--destination")
    _common(quote)
    images = commands.add_parser("images")
    images.add_argument("handle")
    images.add_argument("range", nargs="?")
    config = commands.add_parser("config")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    set_destination = config_commands.add_parser("set-destination")
    set_destination.add_argument("destination")
    config_commands.add_parser("show")
    import_vendors = config_commands.add_parser("import-vendors")
    import_vendors.add_argument("path", type=Path)
    return parser


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--redetect", action="store_true")
    parser.add_argument("--debug", action="store_true")


def _config(data: DataStore, args: argparse.Namespace) -> dict[str, Any]:
    if args.config_command == "set-destination":
        data.set_destination(_json(args.destination, "destination"))
        return {"destination": data.destination()}
    if args.config_command == "show":
        return {**data.settings(), "destination": data.destination(), "data_dir": str(data.root)}
    if args.config_command == "import-vendors":
        return {"imported": data.import_vendors(args.path), "path": str(args.path)}
    raise AssertionError(args.config_command)


def _json(value: str, context: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise ToolError(f"{context} must be valid JSON: {error.msg}") from error


def _has_api_error(value: object) -> bool:
    if isinstance(value, dict):
        return value.get("status") == "api_error" or any(_has_api_error(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_api_error(item) for item in value)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
