from __future__ import annotations


def merge_existing_template_content(
    generated_content: str,
    existing_content: str | None,
) -> str:
    """Restore fresh template instructions while preserving existing content."""
    if existing_content is None:
        return generated_content

    instruction_lines: list[str] = []
    for line in generated_content.splitlines():
        if line.strip() == "" or line.lstrip().startswith("#"):
            instruction_lines.append(line)
            continue
        break

    instruction_prefix = "\n".join(instruction_lines).rstrip()
    if not instruction_prefix:
        return existing_content

    return f"{instruction_prefix}\n{existing_content.rstrip(chr(10))}\n"
