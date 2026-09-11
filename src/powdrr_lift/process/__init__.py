"""The LLM-led process language.

This package owns skills, workflows, templates, tasks, steps, actions, effects,
outcomes, handoffs, and their static safety/liveness contracts. It compiles
source definitions into immutable contracts consumed by the agent and
execution kernel; it does not call providers or execute tools.

The current workflow-definition modules are being migrated here incrementally.
New process-language APIs belong in this package; this marker is intentionally
small until each implementation has a clean ownership boundary.
"""
