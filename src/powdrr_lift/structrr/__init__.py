"""The product and lifecycle language.

This package owns the meaning of product artifacts: requirements, architecture,
implementation intent, proposed changes, current state, and lifecycle records.
It must remain independent of LLM providers, agent orchestration, and runtime
tool adapters.

The current ``powdrr_lift.core`` modules are being migrated here incrementally.
New product-language APIs belong in this package; this marker is intentionally
small until each implementation has a clean ownership boundary.
"""

from powdrr_lift.structrr.bootstrap import (
    BOOTSTRAP_SECTION_VERSIONS,
    BootstrapIssue,
    BootstrapResult,
    BootstrapValidationReport,
    bootstrap_structrr,
    validate_bootstrap_document,
    validate_bootstrap_sections,
)
from powdrr_lift.structrr.proposal import (
    PROPOSAL_REVISION_SCHEMA_VERSION,
    ProposalOperation,
    ProposalRevision,
    compile_proposal_revision,
    load_proposal_revision,
    validate_proposal_revision,
)
from powdrr_lift.structrr.rebase import (
    StructrrChange,
    StructrrRebaseReport,
    StructrrRemapping,
    rebase_structrr_snapshot,
    snapshot_digest,
)

__all__ = [
    "BootstrapIssue",
    "BootstrapResult",
    "BootstrapValidationReport",
    "BOOTSTRAP_SECTION_VERSIONS",
    "bootstrap_structrr",
    "validate_bootstrap_sections",
    "validate_bootstrap_document",
    "StructrrChange",
    "StructrrRebaseReport",
    "StructrrRemapping",
    "rebase_structrr_snapshot",
    "snapshot_digest",
    "PROPOSAL_REVISION_SCHEMA_VERSION",
    "ProposalOperation",
    "ProposalRevision",
    "compile_proposal_revision",
    "load_proposal_revision",
    "validate_proposal_revision",
]
