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
from powdrr_lift.structrr.intent_lineage import (
    INTENT_LINEAGE_HISTORY_SCHEMA,
    audit_intent_lineage,
)
from powdrr_lift.structrr.invariant_review import (
    INVARIANT_REVIEW_PREDICATE_VERSION,
    INVARIANT_REVIEW_RECEIPT_SCHEMA_VERSION,
    InvariantApplicability,
    InvariantOrigin,
    InvariantRequirementBasis,
    InvariantReviewAssessment,
    InvariantReviewEvidencePacket,
    InvariantReviewObligation,
    InvariantReviewReceipt,
    InvariantReviewRun,
    build_invariant_review_result,
    compile_invariant_review_packets,
    compile_invariant_review_receipt,
    compile_invariant_review_worklist,
    load_invariant_review_receipt,
    review_candidate_invariants,
    write_invariant_review_receipt,
    write_invariant_review_run,
)
from powdrr_lift.structrr.proposal_review import (
    PROPOSAL_REVIEW_RECEIPT_SCHEMA_VERSION,
    ProposalReviewReceipt,
    build_structural_review_receipt,
    compile_review_worklist,
    load_review_receipt,
    write_review_receipt,
)
from powdrr_lift.structrr.verification_obligations import (
    VERIFICATION_OBLIGATION_SCHEMA_VERSION,
    AffectedIntentClosure,
    VerificationObligation,
    VerificationObligationCompilation,
    compile_verification_obligations,
)

__all__ = [
    "BootstrapIssue",
    "BootstrapResult",
    "BootstrapValidationReport",
    "BOOTSTRAP_SECTION_VERSIONS",
    "bootstrap_structrr",
    "validate_bootstrap_sections",
    "validate_bootstrap_document",
    "INTENT_LINEAGE_HISTORY_SCHEMA",
    "audit_intent_lineage",
    "INVARIANT_REVIEW_PREDICATE_VERSION",
    "INVARIANT_REVIEW_RECEIPT_SCHEMA_VERSION",
    "InvariantApplicability",
    "InvariantOrigin",
    "InvariantRequirementBasis",
    "InvariantReviewAssessment",
    "InvariantReviewEvidencePacket",
    "InvariantReviewObligation",
    "InvariantReviewReceipt",
    "InvariantReviewRun",
    "build_invariant_review_result",
    "compile_invariant_review_receipt",
    "compile_invariant_review_packets",
    "compile_invariant_review_worklist",
    "load_invariant_review_receipt",
    "review_candidate_invariants",
    "write_invariant_review_run",
    "write_invariant_review_receipt",
    "PROPOSAL_REVIEW_RECEIPT_SCHEMA_VERSION",
    "ProposalReviewReceipt",
    "build_structural_review_receipt",
    "compile_review_worklist",
    "load_review_receipt",
    "write_review_receipt",
    "AffectedIntentClosure",
    "VERIFICATION_OBLIGATION_SCHEMA_VERSION",
    "VerificationObligation",
    "VerificationObligationCompilation",
    "compile_verification_obligations",
]
