"""Canonical fingerprints for Structrr snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


def snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """Return a content address for a Structrr snapshot.

    Mapping keys and semantically unordered snapshot collections are normalized
    before hashing. This makes formatting and YAML ordering irrelevant while
    preserving the values that define the snapshot.
    """
    canonical = _canonical_snapshot(snapshot)
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _canonical_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    collections = {
        "entities",
        "entity_relationships",
        "source_subjects",
        "source_bindings",
    }
    result: dict[str, Any] = {}
    for key, value in snapshot.items():
        if (
            key in collections
            and isinstance(value, Sequence)
            and not isinstance(value, (str, bytes))
        ):
            records = [item for item in value if isinstance(item, Mapping)]
            result[key] = sorted((_canonical(item) for item in records), key=_json_key)
        else:
            result[key] = _canonical(value)
    return result


def _canonical(value: object) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_canonical(item) for item in value]
    return value


def _json_key(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
