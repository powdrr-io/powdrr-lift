# Story execution evidence

Story verification uses the existing MiniSWE test author, collected pytest selectors,
reviewed assertion links, and actual pytest call events. A passed test must execute
all linked assertion locations; skipped/xfail tests, missing calls, setup/teardown
failures, unavailable execution, and changed test sources cannot establish proof.
Semantic review must explicitly accept each required check for the exact contract,
obligations, manifest, and source bytes.

Receipts bind the confirmed story and case, ordered steps/transitions, dependency
digests, collected selector, source digest, review input fingerprint, execution
artifact fingerprint, repository, candidate Git **tree** SHA, configuration, and
runtime environment. The observer builds that tree with a temporary Git index,
including staged ignored generated tests, without changing the caller's index.
Trusted workflow metadata destinations are excluded to avoid self-referential
receipt hashes; the exact exclusions are part of the configuration identity.
Runtime identity includes Python, pytest, installed package versions, and a digest
of environment variables. Receipt files contain those digests, not environment
variable values. Git submodules need an explicit identity adapter and currently
block this observer.

The coverage matrix lists each declared case, ordered transitions and steps,
terminal outcome, required observations, test selectors, executed assertion lines,
and review/receipt identities. Functionality bindings remain visible; unbound steps
need a reviewed public integration boundary justification. Design readiness and
partial implementation remain separate from whole-story verification. Ordinary
implementation PRs keep their own acceptance checks; the compiler's final story
integration item requires every declared case observation.

New behavior requires an actual behavioral baseline failure or a missing public
module that matches an approved added-source operation and observed file absence.
Collection/import/environment errors cannot substitute for that signal. The
compiler-owned existing-behavior integration item permits a passing baseline.
Corrections retain the original policy and receive a new source-bound review.

Issue workers publish `story-execution.json` beside their existing Structrr diff.
`artifact-state --story-evidence` inventories these commit-pinned historical receipts;
it does not assert they describe the current candidate. Local worker and feature
execution also retain each run's raw events, receipts, matrix, and a
`verification-health-input.json` file. Query the latter with:

```sh
powdrr-lift verification-health --input <run>/verification-health-input.json
```

For a current audit, supply the current snapshot, manifest, and execution context
in that input; `--candidate-tree <tree-sha>` can override the expected candidate.
Changed story/case/dependency/test links or candidate/configuration/environment
identities classify old receipts as stale while preserving their recorded result.
An approved amendment needs refreshed authoring, review, and execution before it
can complete the changed story.

`tests/test_story_execution.py` connects a prose interview and confirmed assumption
to a normal proposal, two implementation allocations plus integration, independent
scripted MiniSWE authoring, actual execution, implementation repair, and approved
amendment. Its missing-checkpoint-write defect passes a focused response-only test
but fails the persistence oracle. The same receipt gate is exercised by the feature
endpoint. The scripted provider proves the pipeline contract; it does not establish
live model authoring quality. No live MiniSWE smoke run is implied.
