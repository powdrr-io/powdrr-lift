# Task 1 codebase review: deterministic proposed PR planning

## Status

Complete. The generated system map was populated from current indexed state and
the requested deterministic proposed PR planning change. No product code was
edited.

## Current system findings

- `skill-definitions/start-implementing-feature.yaml` currently combines proposed
  PR records and complete authoritative `section`/`id`/`action` triples in one
  `semantic_specification` output. The model therefore performs PR planning,
  effect transcription, and effect allocation in the same response.
- `src/powdrr_lift/core/pr_specification.py` already provides the correct
  runtime-owned document boundary: it rejects unknown keys, invalid dependency
  graphs, unknown/duplicate/missing effect assignments, and invalid compiled
  specifications before persistence.
- `src/powdrr_lift/workflow_chat_agent.py` already records deterministic
  pre-step results as authoritative handoffs and materializes the proposed PR
  YAML through the compiler under a runtime-owned mutation contract.
- Provider calls currently request only a generic JSON object. Required workflow
  outputs are checked after parsing, so planning/allocation response shapes are
  not yet enforced by a step-specific machine-checkable provider schema.
- Proposed PR evaluation is deterministic, but the workflow still includes a
  model-owned repair/confirmation step that interprets the result before the gate.
  The requested boundary is for runtime code to select success versus repair,
  with structured issues supplied only as repair context.
- `scripts/start-implementing-feature-harness.py` already isolates live runs,
  records transcripts and structured errors, streams progress, and preserves
  repair diagnostics. It is the appropriate end-to-end proof surface for invalid
  split outputs and a valid compiled/evaluated result.

## System map additions

The map preserves all generated indexed state and adds the requested delta:

- separate, independently validated PR-planning and effect-allocation contracts;
- a deterministic structured handoff for authoritative effects;
- allocation by runtime-owned effect reference so the model never transcribes
  authoritative section/id/action triples;
- a split-input compiler that retains runtime ownership of compatibility YAML;
- step-specific machine-checkable output schemas plus existing workflow action
  validation;
- deterministic evaluator-driven success and repair branches;
- no-mutation and exact effect-equivalence invariants; and
- isolated live-harness cases for malformed nesting, malformed allocation,
  incomplete/unknown effects, and valid end-to-end compilation.

## Validation

Generated with:

```text
rtk uv run powdrr-lift system-map-specification --work-item-name deterministic-proposed-pr-planning
```

The required direct evaluation command exposed a repository tooling mismatch:

```text
rtk uv run powdrr-lift evaluate docs/current/deterministic-proposed-pr-planning/system-map-specification.yaml
Unsupported specification-v1 filename: system-map-specification.yaml
```

`powdrr-lift evaluate` recognizes `system-specification.yaml` and suffix variants,
but not the canonical filename produced by `system-map-specification`. To validate
the content without changing the generated artifact, the exact file was copied
to a temporary supported suffix and evaluated with the intended work item:

```text
rtk uv run powdrr-lift evaluate /private/tmp/deterministic-proposed-pr-planning-system-specification.yaml --work-item-name deterministic-proposed-pr-planning --repo-root .
validation_successful: true
issues: []
```

An additional YAML integrity check found 193 ids, zero duplicate ids, 37 entity
relationships, and zero broken relationship source/target references.

## Concern for later tasks

The evaluator/generator filename mismatch should be tracked as a tooling defect.
It does not invalidate the populated content, but native validation of the exact
generated path will remain impossible until `evaluate` recognizes
`system-map-specification.yaml` (or the generator emits a supported name).
