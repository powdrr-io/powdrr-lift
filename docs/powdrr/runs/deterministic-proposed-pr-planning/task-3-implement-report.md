# Task 3 implementation report

## Status

DONE

## Changes

- Split `start-implementing-feature` into independently declared proposed-PR
  planning and effect-allocation model phases.
- Added strict JSON schemas to step output declarations, schema parsing and
  round-trip serialization, active action-envelope derivation, provider request
  propagation, and runtime validation before handoff recording.
- Added a deterministic authoritative-effect pre-step with ordered,
  collision-checked content-derived effect references and exact
  section/id/action handoff values.
- Added split-input proposed-PR compilation with plan, dependency, handoff,
  allocation, reference, coverage, exact-equivalence, and compiled-document
  validation before persistence. The combined compiler remains only as a
  compatibility adapter and is no longer used by the workflow.
- Moved proposed-PR evaluation behind a runtime gate. Success jumps directly to
  workflow-instantiation planning; failure exposes structured issues to the
  semantic repair step. Repair invalidates stale downstream handoffs.
- Extended the isolated live harness with deterministic malformed-plan,
  malformed-allocation, incomplete-allocation, valid-compilation, and
  no-mutation probes; split-phase visibility; document hashes; and final
  evaluator status.
- Updated focused unit and mocked workflow coverage for the split contract,
  schema propagation, provider adapters, deterministic gate routing, and
  end-to-end document/workflow generation.

## Verification

- `rtk .venv/bin/pytest -q tests/test_skill_specification.py tests/test_pr_specification.py tests/test_workflow_llm.py tests/test_workflow_chat_agent.py`
  - 275 passed in 12.07s.
- `rtk .venv/bin/ruff format --check .`
  - 220 files already formatted.
- `rtk .venv/bin/ruff check .`
  - All checks passed.
- `rtk .venv/bin/mypy src tests`
  - Success: no issues found in 168 source files.
- `rtk git diff --check`
  - Passed.
- Harness split-contract probe against `interaction-file-log`
  - All five cases passed; 9 authoritative effects; the existing compatibility
    document remained byte-for-byte unchanged during invalid and valid probes.

## Self-review

- Confirmed planning cannot contain effect allocation or runtime-owned document
  fields under its schema.
- Confirmed allocation can contain only effect references and proposed PR ids.
- Confirmed malformed output validation and semantic compilation occur before
  the current output is recorded as a handoff.
- Confirmed the compiler regenerates and compares the authoritative handoff,
  preserves source ordering, and validates the complete compatibility document
  before the existing atomic mutation boundary writes it.
- Confirmed evaluator success never prompts the repair step in the mocked
  workflow, while evaluator failure retains complete structured issues in
  runtime repair context.
- Confirmed schema-less and legacy clients keep their prior call behavior while
  schema-aware recording, retry, OpenAI-compatible, local, and Anthropic paths
  carry the active schema where supported.

## Concerns

- The live-provider harness itself was not run because this task environment did
  not provide or authorize a live provider invocation. Its deterministic
  contract probe and mocked end-to-end workflow are green.
