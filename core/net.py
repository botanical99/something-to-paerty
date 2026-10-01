"""Small LAN helpers: this machine's address, and QR codes (no internet involved)."""
from __future__ import annotations

import io
import socket


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))       # no packet is sent; this just selects the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def qr_svg(data: str) -> str:
    """QR code as an inline SVG string (pure python, no Pillow needed)."""
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=2, box_size=10)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    import re
    svg = re.sub(r'<\?xml[^>]*\?>\s*', "", svg)
    return re.sub(r'\s(width|height)="[^"]*mm"', "", svg, count=2)
