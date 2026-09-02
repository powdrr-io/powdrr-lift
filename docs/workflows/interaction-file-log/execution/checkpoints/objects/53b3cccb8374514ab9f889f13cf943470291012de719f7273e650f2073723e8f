from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import yaml


def merge_existing_template_content(
    generated_content: str,
    existing_content: str | None,
) -> str:
    """Restore instructions/defaults while preserving valid existing values.

    A generator is also a recovery operation. If the existing file is not
    parseable YAML, the fresh template replaces it. Otherwise the generated
    mapping supplies missing keys and nested defaults while existing values,
    list entries, and additional fields are retained.
    """
    if existing_content is None:
        return generated_content

    try:
        generated_data = yaml.safe_load(generated_content)
        existing_data = yaml.safe_load(existing_content)
    except yaml.YAMLError:
        return generated_content
    if not isinstance(generated_data, Mapping) or not isinstance(
        existing_data, Mapping
    ):
        return generated_content

    merged_data = _merge_template_value(generated_data, existing_data)
    instruction_prefix = _instruction_prefix(generated_content)
    rendered_data = yaml.safe_dump(merged_data, sort_keys=False)
    if instruction_prefix:
        return f"{instruction_prefix}\n{rendered_data}"
    return rendered_data


def _instruction_prefix(generated_content: str) -> str:
    lines: list[str] = []
    for line in generated_content.splitlines():
        if line.strip() == "" or line.lstrip().startswith("#"):
            lines.append(line)
            continue
        break
    return "\n".join(lines).rstrip()


def _merge_template_value(template: Any, existing: Any) -> Any:
    if isinstance(template, Mapping) and isinstance(existing, Mapping):
        merged: dict[Any, Any] = {}
        for key, template_value in template.items():
            if key in existing:
                merged[key] = _merge_template_value(template_value, existing[key])
            else:
                merged[key] = template_value
        for key, existing_value in existing.items():
            if key not in merged:
                merged[key] = existing_value
        return merged
    if isinstance(template, list) and isinstance(existing, list):
        if not template:
            return existing
        prototype = template[0]
        if isinstance(prototype, Mapping):
            return [
                (
                    _merge_template_value(prototype, item)
                    if isinstance(item, Mapping)
                    else item
                )
                for item in existing
            ]
        return existing
    if isinstance(template, Sequence) and not isinstance(
        template, (str, bytes, bytearray)
    ):
        return existing
    return existing
