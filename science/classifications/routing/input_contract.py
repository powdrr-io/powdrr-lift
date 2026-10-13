"""Versioned, metadata-free routing inference serialization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

INPUT_REVISION = "routing-input-v1"
INPUT_KEYS = ("proposition", "local_context", "scope_relations")


def inference_payload(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Project an example's model-visible fields, excluding all annotations."""
    if set(inputs) != set(INPUT_KEYS):
        raise ValueError(f"routing input keys must be exactly {INPUT_KEYS!r}")
    proposition = inputs.get("proposition")
    local_context = inputs.get("local_context")
    scope_relations = inputs.get("scope_relations")
    if not isinstance(proposition, str) or not proposition.strip():
        raise ValueError("routing proposition must be a non-empty string")
    if not isinstance(local_context, Mapping):
        raise ValueError("routing local_context must be an object")
    if scope_relations is not None and not isinstance(scope_relations, Mapping):
        raise ValueError("scope_relations must be an object or null")
    return {
        "proposition": proposition,
        "local_context": dict(local_context),
        "scope_relations": dict(scope_relations)
        if scope_relations is not None
        else None,
    }


def serialize_inference_input(inputs: Mapping[str, Any]) -> bytes:
    """Return canonical UTF-8 JSON for hashing and deterministic model input."""
    return json.dumps(
        inference_payload(inputs),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def inference_input_sha256(inputs: Mapping[str, Any]) -> str:
    return hashlib.sha256(serialize_inference_input(inputs)).hexdigest()
