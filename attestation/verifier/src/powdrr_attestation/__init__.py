"""Portable data contracts for local validation attestations."""

from powdrr_attestation.configuration import (
    AttestorsRegistry,
    ConfigurationError,
    ValidationPolicy,
    parse_attestors_registry,
    parse_validation_policy,
)

__all__ = [
    "AttestorsRegistry",
    "ConfigurationError",
    "ValidationPolicy",
    "parse_attestors_registry",
    "parse_validation_policy",
]
