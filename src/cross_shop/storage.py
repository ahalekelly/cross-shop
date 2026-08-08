from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterator

from platformdirs import user_data_path

from cross_shop.core import DEFAULT_DESTINATION, ToolError

DESTINATION_KEYS = {"country", "region", "city", "address1", "postal_code"}
HANDLE = re.compile(r"^(r[1-9]\d*)\.([1-9]\d*)\.([1-9]\d*)(?:\.([1-9]\d*))?$")


class DataStore:
    def __init__(self, root: Path | None = None) -> None:
        configured = os.environ.get("CROSS_SHOP_DATA_DIR")
        self.root = root or (Path(configured).expanduser() if configured else user_data_path("cross-shop", appauthor=False))
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.RLock()
        self.lock_path = self.root / ".lock"
        self.settings_path = self.root / "settings.json"
        self.vendors_path = self.root / "vendors.json"
        self.runs = self.root / "runs"
        self.images = self.root / "images"
        self.runs.mkdir(exist_ok=True)
        self.images.mkdir(exist_ok=True)
        self._seed_on_first_run()

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self._thread_lock, self.lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def settings(self) -> dict[str, Any]:
        value = self._read(self.settings_path, {})
        if not isinstance(value, dict):
            raise ToolError(f"Settings must be a JSON object: {self.settings_path}")
        return value

    def destination(self) -> dict[str, str]:
        value = self.settings().get("destination", DEFAULT_DESTINATION)
        return validate_destination(value)

    def set_destination(self, destination: object) -> None:
        normalized = validate_destination(destination)
        with self.locked():
            settings = self.settings()
            settings["destination"] = normalized
            self._write(self.settings_path, settings)

    def vendors(self) -> dict[str, dict[str, Any]]:
        value = self._read(self.vendors_path, {})
        if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(item, dict) for key, item in value.items()):
            raise ToolError(f"Vendor registry must be an object map: {self.vendors_path}")
        return value

    def vendor(self, origin: str) -> dict[str, Any] | None:
        return self.vendors().get(origin)

    def upsert_vendor(self, origin: str, value: dict[str, Any]) -> None:
        with self.locked():
            vendors = self.vendors()
            vendors[origin] = value
            self._write(self.vendors_path, vendors)

    def import_vendors(self, path: Path) -> int:
        value = self._read(path, None)
        if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(item, dict) for key, item in value.items()):
            raise ToolError(f"Vendor import must be an object map: {path}")
        with self.locked():
            vendors = self.vendors()
            vendors.update(value)
            self._write(self.vendors_path, vendors)
        return len(value)

    def save_run(self, payload: dict[str, Any]) -> str:
        with self.locked():
            counter_path = self.root / "run-counter"
            raw = counter_path.read_text().strip() if counter_path.exists() else "0"
            if not raw.isdecimal():
                raise ToolError(f"Run counter is invalid: {counter_path}")
            number = int(raw) + 1
            self._write_text(counter_path, str(number))
            run_id = f"r{number}"
            self._write(self.runs / f"{run_id}.json", {"created_at": datetime.now(UTC).isoformat(), **payload})
        self.gc_runs()
        return run_id

    def load_run(self, run_id: str) -> dict[str, Any]:
        path = self.runs / f"{run_id}.json"
        value = self._read(path, None)
        created = value.get("created_at") if isinstance(value, dict) else None
        if not isinstance(created, str):
            raise ToolError(f"Run {run_id} is unavailable; re-run search")
        try:
            created_at = datetime.fromisoformat(created)
        except ValueError as error:
            raise ToolError(f"Run {run_id} is invalid") from error
        if created_at.tzinfo is None:
            raise ToolError(f"Run {run_id} is invalid")
        if created_at < datetime.now(UTC) - timedelta(days=7):
            raise ToolError(f"Run {run_id} expired; re-run search")
        return value

    def resolve_handle(self, handle: str) -> dict[str, Any]:
        match = HANDLE.fullmatch(handle)
        if match is None:
            raise ToolError("Item must be a product-page URL, handle, or ref object")
        run_id, raw_store, raw_item, raw_variant = match.groups()
        run = self.load_run(run_id)
        stores = run.get("stores")
        if not isinstance(stores, list):
            raise ToolError(f"Run {run_id} is invalid")
        store_index, item_index = int(raw_store), int(raw_item)
        if store_index > len(stores) or not isinstance(stores[store_index - 1], dict):
            raise ToolError(f"Handle {handle} has no store {store_index}")
        store = stores[store_index - 1]
        items = store.get("items")
        if not isinstance(items, list) or item_index > len(items) or not isinstance(items[item_index - 1], dict):
            raise ToolError(f"Handle {handle} has no item {item_index}")
        item = dict(items[item_index - 1])
        item["store"] = store.get("store")
        item["handle"] = f"{run_id}.{store_index}.{item_index}"
        variants = item.get("variants", [])
        if raw_variant is not None:
            variant_index = int(raw_variant)
            if not isinstance(variants, list) or variant_index > len(variants) or not isinstance(variants[variant_index - 1], dict):
                raise ToolError(f"Handle {handle} has no variant {variant_index}")
            item["selected_variant"] = variants[variant_index - 1]
            item["handle"] = handle
        return item

    def gc_runs(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=7)
        with self.locked():
            for path in self.runs.glob("r*.json"):
                value = self._read(path, None)
                created = value.get("created_at") if isinstance(value, dict) else None
                if not isinstance(created, str):
                    raise ToolError(f"Run file is invalid: {path}")
                if datetime.fromisoformat(created) < cutoff:
                    path.unlink()

    def _seed_on_first_run(self) -> None:
        if self.vendors_path.exists():
            return
        value = json.loads(
            files("cross_shop").joinpath("vendors.seed.json").read_text()
        )
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not isinstance(item, dict)
            for key, item in value.items()
        ):
            raise ToolError("Vendor seed must be an object map")
        with self.locked():
            if self.vendors_path.exists():
                return
            self._write(self.vendors_path, value)

    @staticmethod
    def _read(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ToolError(f"Unreadable JSON file: {path}") from error

    @staticmethod
    def _write(path: Path, value: object) -> None:
        DataStore._write_text(path, json.dumps(value, sort_keys=True, separators=(",", ":")))

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_text(value)
        os.replace(temporary, path)


def validate_destination(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ToolError("Destination must be a JSON object")
    unknown = set(value) - DESTINATION_KEYS
    if unknown:
        raise ToolError(f"Destination has unknown keys: {', '.join(sorted(unknown))}")
    if not {"country", "postal_code"} <= set(value):
        raise ToolError("Destination requires country and postal_code")
    if any(not isinstance(item, str) or not item.strip() for item in value.values()):
        raise ToolError("Destination values must be nonempty strings")
    country = value["country"].upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ToolError("Destination country must be ISO-3166 alpha-2")
    return {**value, "country": country}
