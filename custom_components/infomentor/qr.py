"""QR-koden som en svartvit bild, oberoende av HA:s tema.

HA:s `ha-qr-code` ritar de mörka rutorna i temats textfärg på kortets bakgrund. I mörkt
tema blir det en inverterad QR-kod (ljus på mörkt) som BankID-appen kan vägra läsa, och
elementet har inget färgalternativ. En markdown-beskrivning får inte heller sätta stil. Så
koden ritas här som en SVG, svart på vitt med en tyst zon, och skickas som en data-URI.
"""

from __future__ import annotations

import base64
import io

import segno

# BankID-QR-data är en kort sträng; låg felkorrigering ger färre, större rutor och en
# kod som är lätt att skanna från en skärm.
_ERROR_LEVEL = "l"
_SCALE = 8
_QUIET_ZONE_MODULES = 4


def qr_markdown_image(data: str) -> str:
    """`![](data:image/svg+xml;base64,...)` för `data`, svart på vitt."""
    qr = segno.make(data, error=_ERROR_LEVEL, micro=False)
    out = io.BytesIO()
    qr.save(
        out,
        kind="svg",
        scale=_SCALE,
        border=_QUIET_ZONE_MODULES,
        dark="#000000",
        light="#ffffff",
        xmldecl=False,
        nl=False,
    )
    encoded = base64.b64encode(out.getvalue()).decode("ascii")
    return f"![BankID](data:image/svg+xml;base64,{encoded})"
