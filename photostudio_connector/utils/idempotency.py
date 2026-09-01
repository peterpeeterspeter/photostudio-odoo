"""Pure idempotency helpers (no Odoo imports — standalone-testable)."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def deterministic_key(*parts: Any) -> str:
    normalized = ":".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def bool_salt(value: bool) -> str:
    return "1" if value else "0"


def body_without_client_reference(body: dict[str, Any]) -> dict[str, Any]:
    clone = deepcopy(body)
    metadata = clone.get("metadata")
    if isinstance(metadata, dict):
        metadata = dict(metadata)
        metadata.pop("client_reference", None)
        clone["metadata"] = metadata
    return clone


def build_batch_idempotency_key(
    database_uuid: str,
    replace_strategy: str,
    auto_publish: bool,
    body: dict[str, Any],
    regenerate_nonce: int = 0,
) -> str:
    minimized = body_without_client_reference(body)
    return deterministic_key(
        database_uuid,
        replace_strategy,
        bool_salt(auto_publish),
        str(int(regenerate_nonce)),
        canonical_json(minimized),
    )


def attach_client_reference(body: dict[str, Any], key: str) -> dict[str, Any]:
    clone = deepcopy(body)
    metadata = clone.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
        clone["metadata"] = metadata
    metadata["client_reference"] = f"odoo:{key[:24]}"
    return clone
