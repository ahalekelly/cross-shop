from __future__ import annotations

import httpx
import pytest

from cross_shop.core import wall_system

BODIES = [
    ("cloudflare", 503, {}, "<title>Just a moment...</title>"),
    ("akamai", 403, {"server": "AkamaiGHost"}, "Access Denied"),
    ("datadome", 403, {}, "<script src='https://js.datadome.co/tags.js'></script>"),
    ("perimeterx", 403, {}, "<div id='px-captcha'></div>"),
    ("perimeterx", 403, {"set-cookie": "_pxhd=abc; Path=/"}, "Access to this page has been denied"),
    ("kasada", 429, {"x-kpsdk-ct": "token"}, "retry"),
    ("imperva", 403, {"x-iinfo": "9-12345-0 NNNN"}, "Request unsuccessful."),
    ("imperva", 403, {}, "<html>Powered by Incapsula</html>"),
    ("aws_waf", 405, {}, "<script src='https://d1.awswaf.com/challenge.js'></script>"),
    ("captcha", 403, {}, "Please verify you are human before continuing"),
]
ORDINARY = [
    "<html><body><h1>Pliers</h1><p>In stock</p></body></html>",
    "<html><body>Our privacy policy explains cookies and tracking pixels.</body></html>",
    '<html><body><img src="https://cdn.example/px/hero.png"></body></html>',
]


@pytest.mark.parametrize(("system", "status", "headers", "body"), BODIES)
def test_wall_markers_name_the_system(system: str, status: int, headers: dict[str, str], body: str) -> None:
    response = httpx.Response(status, headers=headers, text=body)
    assert wall_system(response) == system


@pytest.mark.parametrize("body", ORDINARY)
def test_ordinary_pages_are_not_walls(body: str) -> None:
    assert wall_system(httpx.Response(200, text=body)) is None
