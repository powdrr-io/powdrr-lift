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
first run emitted standalone list markers (`2.`, `3.`, and so on) as clauses;
the opening two-deliverable clause was also duplicated by the model's atomicity
split. That split duplicated its statement, while atomicity children correctly
inherit their parent source span. The parser should treat numbered markers as
structure; atomicity processing should separately guard against repeated
children.

The instruction also provides two distinct deliverables (formatter behavior
and a `sqlfmt.ddl` module), eight numbered formatting requirements, explicit
out-of-scope pass-through behavior, public data models, parser acceptance of
arbitrary valid parsed representations, and an instruction to commit. The
design retained both product areas and the out-of-scope clauses, while the
commit directive remained non-product process context. This is a useful
classification example for mixed product, API, test-oracle, exclusion, and
workflow instructions within one Python task.

The rerun after list-aware segmentation reduced the atomic ledger from 56 to 46
clauses and the canonical design from 52 to 44 obligations. No standalone
number markers appeared. The opening deliverable clause split into two distinct
statements on this attempt, though duplicate split children still lack a
deterministic guard. The prompt and Structrr diff were generated successfully;
the diff contains three features, 39 invariants, 44 acceptance criteria, and 44
required test cases, but still no entity or relationship changes. The original
instruction is present verbatim in the prompt, but `instruction-044` (collect
all table constraints, including bare CHECK and named CONSTRAINT forms) maps to
a generic acceptance criterion and test oracle. This is a remaining semantic
coverage gap beyond sentence segmentation.

The atomicity compiler now rejects duplicate split children after Unicode,
Markdown, whitespace, and case normalization. It keeps the original parent
clause and records a `duplicate_child` diagnostic with the repeated child
indexes and source span; Markdown-only output that normalizes to empty is also
rejected. Identical generated text under distinct source spans remains intact.
The compiler cannot yet prove that paraphrased children cover every source
detail: split responses contain rewritten statements but no source-alignment
spans, and accepted children currently inherit their parent's span. A future
split contract needs validated source ranges before deterministic coverage
checks can be added.
