# Coding loop

Objective: add `coding_loop` as a first-class workflow step type with typed
configuration, bounded iterations, verification guidance, and durable tests.

## Tasks

- [x] Map the current step model, validation, prompting, and execution flow.
- [x] Add the typed coding-loop configuration and serialization.
- [x] Integrate bounded coding-loop behavior into execution and prompts.
- [x] Add focused tests and update templates/documentation.
- [x] Run validation and prepare the PR changelog.

## Decisions

- `coding_loop` remains an agent-controlled step, but the harness owns its
  iteration bound and exposes verification requirements in the prompt.
- Verification entries are structured objects so they can later be connected to
  deterministic commands without changing the step schema.
- The shared runner resets the coding-loop budget when the active step changes
  and records an explicit exhaustion event at the bound.

## Verification

- Focused tests: 94 passed.
- Full tests: 800 passed.
- Ruff format and lint: passed.
- Basedpyright still reports the repository's existing baseline diagnostics.
