"""Private process definitions and other packaged service resources."""

from pathlib import Path


def bundled_process_directory() -> Path:
    """Return the installed service process catalog directory."""
    return Path(__file__).resolve().parent / "processes"
