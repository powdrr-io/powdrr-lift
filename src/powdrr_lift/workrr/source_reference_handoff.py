"""Compatibility exports for the service-owned placement handoff compiler."""

from powdrr_service.structrr.source_reference_handoff import (
    CompiledSourceReferenceHandoff,
    SourceReferencePrompt,
    compile_source_reference_handoff,
)

__all__ = [
    "CompiledSourceReferenceHandoff",
    "SourceReferencePrompt",
    "compile_source_reference_handoff",
]
