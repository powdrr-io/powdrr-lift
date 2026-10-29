"""Compatibility import for service-owned bounded question generation."""

from powdrr_service.planning import questions as _questions
from powdrr_service.planning.questions import *  # noqa: F403

batch_question_requests = _questions.batch_question_requests
__all__ = tuple(dict.fromkeys((*_questions.__all__, "batch_question_requests")))
