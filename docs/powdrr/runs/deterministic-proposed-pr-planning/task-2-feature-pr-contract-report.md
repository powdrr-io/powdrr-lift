# Task 2 report: Feature and proposed PR contract

## Status

Complete. The feature/PR specification defines one cohesive implementation PR
that separates PR planning from effect allocation while preserving the existing
runtime-owned proposed PR YAML contract.

## Contract decisions

- PR decomposition produces only ordered PR definitions and dependencies.
- A deterministic handoff supplies authoritative effects with stable references.
- Allocation returns only effect references and target PR ids.
- Step output schemas fail closed before handoff recording or mutation.
- The split-input compiler retains exact effect equivalence and validate-before-write
  behavior.
- Evaluator results select success or repair routes in runtime code.
- Unit, mocked workflow, and isolated live-harness evidence cover invalid and valid
  paths.

The code-edit-context reports show that this plan preserves the intent behind
explicit step handoffs, deterministic pre-steps, atomic workflow advancement,
unified proposed PR documents, and runtime-owned compilation. It supersedes only
the combined semantic response that requires the model to transcribe authoritative
effect triples.

## Validation

Generated with the repository `feature-pr-specification` command, filled from the
system map and current source, and validated with:

```text
rtk uv run powdrr-lift evaluate docs/proposals/deterministic-proposed-pr-planning/feature-pr-specification.yaml --work-item-name deterministic-proposed-pr-planning --repo-root .
validation_successful: true
issues: []
```

No product code was edited in this task.
