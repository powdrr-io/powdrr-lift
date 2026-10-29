"""Compatibility import for service-owned bounded question generation."""

from powdrr_service.planning import questions as _questions
from powdrr_service.planning.questions import *  # noqa: F403

__all__ = _questions.__all__
