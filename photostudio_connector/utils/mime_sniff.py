"""Sniff image MIME from base64 payload (no Odoo imports)."""

from __future__ import annotations

import base64
import binascii


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
WEBP_RIFF = b"RIFF"
WEBP_MARKER = b"WEBP"
JPEG_SOI = b"\xff\xd8"


def sniff_image_mime(base64_data: str, declared_mime: str = "image/jpeg") -> str:
    if not base64_data:
        return declared_mime or "image/jpeg"
    try:
        raw = base64.b64decode(base64_data, validate=True)
    except (binascii.Error, ValueError):
        return declared_mime or "image/jpeg"
    if raw.startswith(PNG_SIGNATURE):
        return "image/png"
    if raw.startswith(JPEG_SOI):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == WEBP_RIFF and raw[8:12] == WEBP_MARKER:
        return "image/webp"
    return declared_mime or "image/jpeg"
