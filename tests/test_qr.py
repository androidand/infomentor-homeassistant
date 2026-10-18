"""QR-bilden: svart på vitt med tyst zon, oberoende av HA:s tema."""

from __future__ import annotations

import base64
import re

import segno

from custom_components.infomentor.qr import qr_markdown_image

DATA = "bankid.5a637711-566a-4488-9c72-b8694eda1260.3.278f0d0feca76a98"


def _svg(markdown: str) -> str:
    match = re.fullmatch(r"!\[BankID\]\(data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)\)", markdown)
    assert match, markdown[:80]
    return base64.b64decode(match.group(1)).decode()


def test_is_an_svg_image_black_on_white() -> None:
    svg = _svg(qr_markdown_image(DATA))
    assert svg.startswith("<svg")
    # segno skriver färgerna i kortform: ljus bakgrund som fill, mörka rutor som stroke.
    assert re.findall(r'(fill|stroke)="([^"]+)"', svg) == [("fill", "#fff"), ("stroke", "#000")]
    # Inget i bilden får bero på temat (HA:s ha-qr-code gör det och ger ljus-på-mörkt).
    assert "currentColor" not in svg
    assert "var(" not in svg


def test_has_a_quiet_zone_and_the_scale_the_scanner_needs() -> None:
    svg = _svg(qr_markdown_image(DATA))
    width, height = segno.make(DATA, error="l", micro=False).symbol_size(scale=8, border=4)
    assert f'width="{width}"' in svg
    assert f'height="{height}"' in svg


def test_different_data_gives_a_different_image() -> None:
    assert qr_markdown_image(DATA) != qr_markdown_image(DATA.replace(".3.", ".4."))


def test_image_is_small_enough_to_refresh_every_second() -> None:
    assert len(qr_markdown_image(DATA)) < 20_000
