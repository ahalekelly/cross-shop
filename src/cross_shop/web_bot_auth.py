from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cross_shop.core import ToolError

SIGNATURE_HEADER_NAMES = ("Signature-Agent", "Signature-Input", "Signature")


def build_signer(settings: object) -> Callable[[httpx.Client, httpx.Request], httpx.Response] | None:
    if settings is None:
        return None
    if not isinstance(settings, dict) or set(settings) != {"private_key_path", "key_directory_url"}:
        raise ToolError("web_bot_auth must contain exactly private_key_path and key_directory_url")
    raw_path = settings["private_key_path"]
    directory = settings["key_directory_url"]
    if not isinstance(raw_path, str) or not isinstance(directory, str):
        raise ToolError("web_bot_auth values must be strings")
    path = Path(raw_path).expanduser()
    try:
        private_key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError) as error:
        raise ToolError(f"Web Bot Auth private key is unreadable: {path}") from error
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ToolError(f"Web Bot Auth private key is not Ed25519: {path}")
    key_id = _key_id(private_key)
    parsed = httpx.URL(directory)
    if parsed.scheme != "https" or not parsed.host or parsed.userinfo:
        raise ToolError("Web Bot Auth key_directory_url must be an absolute HTTPS URL")

    def send(client: httpx.Client, request: httpx.Request) -> httpx.Response:
        for name in SIGNATURE_HEADER_NAMES:
            if name in request.headers:
                raise ToolError(f"Web Bot Auth request already contains {name}")
        request.headers.update(_signature_headers(request.url, private_key, key_id, directory))
        return client.send(request, follow_redirects=False)

    return send


def _signature_headers(target: httpx.URL, private_key: Ed25519PrivateKey, key_id: str, directory: str) -> dict[str, str]:
    if target.scheme != "https" or not target.host or target.userinfo:
        raise ToolError("Web Bot Auth target must be an absolute HTTPS URL without credentials")
    host = f"[{target.host}]" if ":" in target.host else target.host.encode("idna").decode()
    authority = host if target.port is None else f"{host}:{target.port}"
    created = int(time.time())
    nonce = base64.b64encode(secrets.token_bytes(64)).decode()
    signature_agent = json.dumps(directory)
    parameters = (
        f'("@authority" "signature-agent");created={created};keyid="{key_id}";alg="ed25519"'
        f';expires={created + 60};nonce="{nonce}";tag="web-bot-auth"'
    )
    base = f'"@authority": {authority}\n"signature-agent": {signature_agent}\n"@signature-params": {parameters}'
    signature = base64.b64encode(private_key.sign(base.encode())).decode()
    return {"Signature-Agent": signature_agent, "Signature-Input": f"sig1={parameters}", "Signature": f"sig1=:{signature}:"}


def _key_id(private_key: Ed25519PrivateKey) -> str:
    public = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    jwk = json.dumps({"crv": "Ed25519", "kty": "OKP", "x": _base64url(public)}, separators=(",", ":"), sort_keys=True).encode()
    return _base64url(hashlib.sha256(jwk).digest())


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")
