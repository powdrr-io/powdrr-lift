# Requirement-scoped GraphQL prompt follow-up

## Run and outcome

This is a prompt-generation experiment using the same 23 frozen routed
requirements as `requirement-contract-v3`, now with one full-index source packet
per requirement from `requirement-evidence-v3`. The task base was
`f07c89f8f065010a36b4263eded209b2b1d37063`, and it remained clean. Flash and Jev
completed all 115 provider calls with no provider or evaluator errors. No tests
or coding agents ran.

The renderer accepted **0 of 23** proposed criteria. `prompt.md` therefore
retains the original requirements and contains no added acceptance criteria.
This is a completed provider run but a failed prompt outcome; it cannot support
a claim that requirement-scoped retrieval improved the final prompt.

The local query checker found 8 invalid examples: 6 place `@defer` on a field
instead of a fragment spread or inline fragment, and 2 contain malformed GraphQL
documents. Across the proposals, premise decisions were 3 supported, 12 unknown
and 8 contradicted; outcome reviews were 7 supported, 15 unknown and 1
contradicted. The render gate requires a valid example plus supported premise
and review, so no proposal passed all checks. This gate correctly withheld
invalid or under-justified criteria; the pipeline has no successful criterion
to compare to the solution.

## What source retrieval changed

The packets recovered the declarations missed by batch reranking: r022 contains
the fragment and fragment-spread APIs, and r023 contains `DSLField` and its
public methods, including `.directives`. One proposal for r023 correctly
constructs a query with the requested directive arguments, but it combines the
DSL AST condition with unrelated server streaming behavior; Jev marked the
outcome unknown. This shows that better source availability did not resolve
what the acceptance criterion should assert.

Retrieval still has false positives: r023 carries an unrelated upload test, and
r015 selects only a multipart test helper. At the time of this run, dependency
directive definitions were filtered using only the routed clause text, so a
broader API clause could lead the model to use `@defer` without receiving its
directive-location evidence.

## Static solution comparison (after generation)

The solution and validation patches were read only after all provider calls and
were never passed to a model. The validation patch adds 18 test functions
covering deferred-fragment merging, nested list paths, list insertion and nulls,
concurrent delivery, errors, root-level updates, non-incremental responses,
WebSockets, DSL directives, generator cleanup, and schema integration. The
fallback prompt contains none of these as newly generated acceptance criteria;
it preserves only the original requirement statements.

Some raw proposals point toward correct behaviors without becoming prompt
criteria. For example, r010 says the second streamed item belongs at index 1,
but its query is malformed. r008 describes merging a deferred `data` object at
the parent path, but its premise remained unknown. r022 mixes DSL API existence,
AST placement, and execution outcomes, and its example places `@defer` on a
field. These are draft observations, not accepted or solution-validated
criteria.

The prior `atomic-contract-v1` emitted five criteria and its manual audit found
all five unusable. The current run's zero accepted criteria is a safer
abstention, but the experiments are not controlled comparisons: the workflow
and acceptance gates changed as well as the evidence source.

## Diagnosis

The retrieval miss is fixed for the named DSL receivers, but source selection
was not the blocking stage in this run. Setup construction and criterion review
remain under-specified: model proposals often choose an invalid GraphQL example
or assert behavior that the per-requirement evidence cannot justify. The
no-criteria fallback keeps the run headless and preserves the original request,
but does not create a more useful prompt. A further full run without changing
those decisions would repeat the same experiment rather than test a new
hypothesis.

All raw calls, intermediate decisions, evidence records and the rendered
fallback are preserved in `checkpoints.tar.gz`.

## Follow-up: preserve instruction context upstream

The captured r003 clause names `session.execute_incremental(query)` but its
`focused_sources` result omitted the dependency declarations for `@defer` and
`@stream`, because directive retrieval checked only the child clause text. The
full frozen instruction names both directives. The resolver now selects
dependency directive declarations from explicit directive names anywhere in the
instruction, and records that evidence source in the returned packet. An offline
check confirms r003 now selects both names; no provider calls or prompt rerun
were made, so this change has not yet been evaluated for criterion quality.

The production atomicity splitter also now receives the source sentence and up
to one adjacent sentence on each side. Its request scopes that text to resolving
references, conditions, and shared terminology, so nearby requirements are not
added to the split result. The ledger still binds each child to its original
source span for downstream classification and auditing.
