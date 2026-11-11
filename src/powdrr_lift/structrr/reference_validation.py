"""Compatibility exports for independent service-owned reference validation."""

from powdrr_service.structrr.reference_validation import (
    ReferenceReviewClient,
    ReferenceValidationReport,
    validate_source_references,
    validation_input_identity,
)

__all__ = [
    "ReferenceReviewClient",
    "ReferenceValidationReport",
    "validate_source_references",
    "validation_input_identity",
]
