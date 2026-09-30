# Python instruction-to-prompt audit: Bandit taint analysis

This host-side probe ran the unchanged DeepSWE instruction for
`bandit-interprocedural-taint-checks` through Structrr bootstrap and the
Harbor-feature design-only pipeline. No coding agent or task verifier was run.
The task checkout remained clean. Artifacts are retained under
`/private/tmp/python-instruction-audit-20260930/bandit/`.

The run compiled 26 instruction clauses into 23 product obligations, rendered a
33,579-byte worker prompt, and recorded 704 Procedrr events (387 judge calls).
Design compilation took about nine minutes. JEV requests in the run generally
completed in a few hundred milliseconds; planning and source-faithfulness work
accounted for most of the elapsed time.

Structrr bootstrap discovered 288 repository entities. Its validation tooling
sections were empty, so the prompt had no validation profiles or allowed
commands; the provider registry still generated required test selectors.
Bootstrap entities were not included in the root instruction classifier's
context. The design-only run did not emit a Structrr diff automatically. A diff
draft was projected from the canonical obligations for audit: 6 features, 16
invariants, 1 guidance item, 23 acceptance criteria, and 23 required test
cases. It contains no entity additions, so it is an obligation projection
rather than a complete repository-entity diff. The follow-up change in this
branch now emits that projection automatically beside the design-only worker
prompt.

The first source sentence describes the current defect: Bandit misses taint
flows through variables. Initially, the classifier turned that sentence into an
obligation to leave the flows undetected, contradicting the later requirements
to flag those flows. Supplying the complete instruction ledger to root routing
corrected the clause to `context`; the other 23 actionable obligations remain
in the compiled design. The worker prompt and Structrr draft from the corrected
run are in `pipeline-ledger-context/`.

The remaining design gap is use of the bootstrapped entity inventory during
classification and diff generation. The current diff projection maps feature
obligations, but does not propose additions or changes to named repository
entities such as Bandit plugins B620–B624.
