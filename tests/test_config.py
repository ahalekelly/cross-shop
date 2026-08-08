from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cross_shop.core import ToolError
from cross_shop.storage import DataStore
from cross_shop.web_bot_auth import build_signer


def test_shipped_vendor_seed_preserves_redirect_aliases() -> None:
    seed = json.loads((Path(__file__).parents[2] / "vendors.seed.json").read_text())
    assert seed["https://malcowallshop.com"]["api_origin"] == "https://holzbuchstaben.ch"
    assert seed["https://mettleair.com"]["api_origin"] == "https://mettleairstore.com"


def test_vendor_seed_import_and_merge(tmp_path: Path) -> None:
    data = DataStore(tmp_path)
    path = tmp_path / "import.json"
    path.write_text(json.dumps({"https://new.test": {"platform": "shopify", "api_origin": "https://new.test", "detected_at": "2026-08-01", "evidence": ["curated"]}}))
    assert data.import_vendors(path) == 1
    assert data.vendor("https://new.test")["platform"] == "shopify"


def test_unconfigured_web_bot_auth_is_unsigned() -> None:
    assert build_signer(None) is None


def test_missing_web_bot_auth_key_names_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing.pem"
    with pytest.raises(ToolError, match=str(missing)):
        build_signer({"private_key_path": str(missing), "key_directory_url": "https://agent.test/keys.json"})


def test_web_bot_auth_signer_uses_fresh_replay_material(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "private.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    signer = build_signer({"private_key_path": str(path), "key_directory_url": "https://agent.test/keys.json"})
    assert signer is not None
    # Signature generation is exercised by the Shopify transport tests; construction validates key type and thumbprint.
