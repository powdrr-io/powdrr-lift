# Task 3: Implement deterministic proposed PR planning

Implement the validated feature contract in
`docs/proposals/deterministic-proposed-pr-planning/feature-pr-specification.yaml`.

## Required behavior

- Split the current combined `semantic_specification` response into a strict
  planning output and a later strict allocation output.
- Planning owns only ordered PR `id`, `intent`, `justification`, and
  `dependent_pr_ids` values.
- Runtime code loads authoritative effects and exposes collision-checked stable
  effect references plus exact section/id/action values as a structured handoff.
- Allocation returns only every stable `effect_ref` once and its target
  `proposed_pr_id`; it never transcribes section/id/action.
- Extend step output declarations with optional machine-checkable JSON schemas,
  validate them before recording handoffs or mutation, and pass the active
  action-envelope schema through the shared LLM request/provider path where
  supported.
- Compile the validated plan, allocation, and runtime effects into the existing
  read-only YAML with exact effect equivalence and validate-before-write behavior.
- Make evaluator success/failure routing runtime-owned; remove model confirmation
  from the deterministic success path and provide structured issues to repair.
- Extend the isolated start-feature harness with visible split-phase progress and
  malformed/valid evidence.

## Compatibility intent

The code-edit-context review requires preserving explicit step handoffs,
deterministic pre-steps, atomic tool-step advancement, unified proposed PR
documents, provider recording/retry behavior, and runtime-owned validation and
persistence. Supersede only the combined semantic contract and the model-owned
validation confirmation branch.

## Expected files

- `skill-definitions/start-implementing-feature.yaml`
- `src/powdrr_lift/core/skill_specification.py`
- `src/powdrr_lift/core/pr_specification.py`
- `src/powdrr_lift/workflow_llm.py`
- `src/powdrr_lift/workflow_chat_agent.py`
- `scripts/start-implementing-feature-harness.py`
- focused tests under `tests/`

If another existing source file must change, stop and ask the primary agent to
run code-edit-context for its exact ranges before editing it.

## Acceptance and verification

- Invalid planning/allocation shapes and semantic references fail before handoff
  recording and leave an existing output file byte-for-byte unchanged.
- A valid split input preserves authoritative effect ordering and exact values.
- Successful evaluation advances without an LLM repair call; failure routes to
  semantic repair with structured issues.
- Update all focused tests and run them.
- Run formatting on touched Python files and `git diff --check`.
- Write a durable implementation report, update only Task 3 in the ledger, and
  commit the complete task on the feature branch. Do not push or open a PR.
