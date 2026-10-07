# Prompt quality recovery evidence

`criterion-wire-v2-example.json` is a hand-authored structural example. It is
not a live model response and is not evidence of improved prompt quality.

The compiler regression fixture
`tests/fixtures/prompt_quality_recovery/captured-prose-criterion.json` preserves
the sanitized criterion output from capture revision
`4394a2c88d0c76cf4b121df1212ee713bcbceeff` for task
`gql-incremental-graphql-delivery`. Its source excerpt is included alongside the
response. The captured response is prose even though the old provider schema
accepted any string in `criteria`; the compiler then attempted to decode each
string as JSON. This is evidence of the wire-format failure only. The capture
had no completed coding or verifier result and cannot establish solution
correctness.

The initial typed example is structural test data only. It must not be reported
as a live model quality improvement.

## PR 1 live capture

The full GraphQL task was captured in prompt-only mode at target revision
`f07c89f8f065010a36b4263eded209b2b1d37063`; the unchanged instruction SHA-256 is
`0d14a5dac7fa2d6fe2e9d9d9a113fb02cfb2495f63f8b7edcaa18ba73dd16edf`. The
provider-ready worker prompt is the entry indexed by
`artifacts/prompts/index.json`, with SHA-256
`9f29bcac3f18f6a1c25831523ebe903b3aae5140b69a11edff47de625a48011f` and size
21,578 bytes. Its separate `implementation-prompt.md` projection is 14,095 bytes
and is not the worker prompt.

Compared with the saved capture from Powdrr revision
`4394a2c88d0c76cf4b121df1212ee713bcbceeff` on the same target commit and
instruction bytes:

| Measure | Saved capture | PR 1 capture |
| --- | ---: | ---: |
| Indexed worker prompt bytes | 11,514 | 21,578 |
| Source requirements | 16 | 22 |
| Bound criteria | 0 | 31 unique IDs |
| Checkable criteria | 0 | 0 |
| Requirement coverage | 16 source-only | 22 unresolved |
| Criterion repair rounds | 2 per failed source requirement | 2 |

The increased requirement count reflects different instruction splitting and
classification in the newer pipeline, so the raw counts are not directly
comparable as a coverage score. The new prompt explicitly says, “No behavior
scenario has yet been assessed as a checkable acceptance criterion.” It also
marks 38 assertions as `decision_required`; all 31 generated criteria remain
unresolved after two bounded repair rounds. This run proves that typed criterion
objects bind and survive downstream processing without string-decoding errors.
It does **not** show improved checkable acceptance coverage or establish
semantic prompt quality. These unresolved meanings are evidence for the
interpretation and routing work in later PRs.

The event log contains 590 successful provider-attempt records and no recorded
retry attempts. Provider cost and end-to-end duration were not emitted in the
run report; record both as unavailable rather than estimating them. The first
capture stopped on duplicate criterion IDs. The same run was resumed from saved
judge responses after adding compiler-side deduplication; the final indexed
prompt was captured successfully. No coding agent or benchmark worker was run.
