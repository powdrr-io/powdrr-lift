from __future__ import annotations

ENTITY_ACTIONS = frozenset({"added", "removed", "changed"})

ACTION_ALIASES = {
    "add": "add",
    "added": "add",
    "change": "change",
    "changed": "change",
    "altered": "change",
    "modified": "change",
    "remove": "remove",
    "removed": "remove",
    "deleted": "remove",
}


def normalize_action(value: object) -> str:
    """Normalize persisted legacy spellings without losing modifications."""
    if not isinstance(value, str) or value not in ACTION_ALIASES:
        raise ValueError(f"unsupported Structrr action: {value!r}")
    return ACTION_ALIASES[value]
