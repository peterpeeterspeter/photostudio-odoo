"""Output URL origin pinning (no Odoo imports — standalone-testable)."""

from __future__ import annotations

from urllib.parse import urlparse


def parse_origin(url: str) -> str | None:
    if not url:
        return None
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def is_allowed_output_url(output_url: str, api_url: str) -> bool:
    allowed = parse_origin(api_url)
    if not allowed:
        return False
    output_origin = parse_origin(output_url)
    return output_origin == allowed
