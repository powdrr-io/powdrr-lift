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

## SQLite import checkpoints

The design-only pipeline was also run against
`sqlite-utils-safe-import-checkpoints`; no coding agent or verifier was run.
The task worktree and artifacts are retained under
`/private/tmp/python-instruction-audit-20260930/sqlite-utils/`.

The task contains 15 instruction clauses. Atomicity processing produced 39
source clauses, of which 38 became product obligations and one was retained as
context. The design-only run completed and emitted both
`implementation-prompt.md` and `structrr-diff.yaml`. The diff contains one
feature, 37 invariants, 38 acceptance criteria, and 38 required test cases, but
no entity or relationship changes. Bootstrap found 89 entities, two tools, and
two validation inventory records; the inventory is present in bootstrap but
does not link the proposed API and CLI changes to existing repository entities.

This run exposed two malformed-but-recoverable atomicity group shapes from the
model: overlapping `all_together` groups and a path-prefixed group string. The
ledger compiler now merges overlapping groups only when all relations are
`all_together`, strips the stray `./` prefix, and drops singleton groups because
they specify no relationship. It still rejects overlaps involving ordered,
conditional, or alternatives relations. Regression tests cover the merge,
singleton cleanup, and rejection behavior.

The local validation profile discovered pytest, but task test collection could
not import `sqlite_fts4` from the shared environment. This prevented reliable
test-selector discovery; it did not prevent design compilation. The run
recorded 1,001 Procedrr events, including 190 dependent decisions and 193
field-entailment decisions. JEV was enabled and returned accepted responses.

## SQLFmt DDL formatter and parser

The design-only pipeline was run against `sqlfmt-create-table-ddl-formatting`;
no coding agent or verifier was run. The initial attempt exposed a bootstrap
failure on a tracked symlink to a directory. Bootstrap now includes only
tracked paths that resolve to files, preserving symlinks to files while
excluding directory targets. A regression test covers the directory case. The
successful rerun and initial failure are retained under
`/private/tmp/python-instruction-audit-20260930/sqlfmt/`.

The successful run produced an implementation prompt and Structrr diff from 56
ledger clauses, 52 product obligations, and 1,359 Procedrr events. Bootstrap
found 231 entities, four tools, and four validation inventory records. As in
the other audits, the Structrr diff contains no entity or relationship changes.
Pytest selector discovery could not import the task package `sqlfmt` from the
shared environment, but design generation completed and JEV returned accepted
responses.

This task exposed a source-segmentation gap in Markdown numbered lists. The
ledger duplicated the opening two-deliverable sentence with the same source
span, assigned the same source span to two separate requirements, and emitted
standalone list markers (`2.`, `3.`, and so on) as clauses. Those markers then
produced generic or undefined design scenarios. The full instruction remains
in the worker prompt and the individual substantive rules are mostly present,
but the extra clauses create noise and make source-to-obligation traceability
unreliable. The parser should treat numbered markers as structure, preserve
paragraph/list-item spans accurately, and deduplicate only when source spans
and text genuinely identify the same source clause.

The instruction also provides two distinct deliverables (formatter behavior
and a `sqlfmt.ddl` module), eight numbered formatting requirements, explicit
out-of-scope pass-through behavior, public data models, parser acceptance of
arbitrary valid parsed representations, and an instruction to commit. The
design retained both product areas and the out-of-scope clauses, while the
commit directive remained non-product process context. This is a useful
classification example for mixed product, API, test-oracle, exclusion, and
workflow instructions within one Python task.
