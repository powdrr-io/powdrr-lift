"""The product and lifecycle language.

This package owns the meaning of product artifacts: requirements, architecture,
implementation intent, proposed changes, current state, and lifecycle records.
It must remain independent of LLM providers, agent orchestration, and runtime
tool adapters.

The current ``powdrr_lift.core`` modules are being migrated here incrementally.
New product-language APIs belong in this package; this marker is intentionally
small until each implementation has a clean ownership boundary.
"""
