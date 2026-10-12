# Complete prompt comparison: Koota composite trait aspects

## Decision

**Do not replace the earlier detailed prompt with this new candidate.** It
preserves the original product requests and removes incorrect prescriptions,
but supplies substantially less guidance about the behavior in the solution.
Removing synthesis is useful for assembly. The three-decision analysis feeding
this candidate is insufficient for a complete, low-ambiguity specification.

| Saved artifact | Fully specified target bundles | Partial | Missing | Wrong |
| --- | ---: | ---: | ---: | ---: |
| Earlier hand-written candidate | 11 | 8 | 14 | 1 |
| Previous LLM-generated prompt | 19 | 7 | 5 | 3 |
| New complete candidate | 3 | 16 | 15 | 0 |
| Same-analysis direct-rendering control | 15 | 9 | 8 | 2 |
| Ideal reference from frozen solution audit | 34 | 0 | 0 | 0 |

These are static agent judgments on the **same 34 existing target bundles**,
not independent requirements, test counts, or coding pass rates. Wrong takes
priority over partial coverage when a bundle contains an explicit contradiction.
Exact quotes and reasons are in [comparison.json](comparison.json); all target
rows are in [coverage-matrix.md](coverage-matrix.md). The ideal's coverage is
complete for these entries by construction, not an experimental result.

The patch review also found a callback-arity mismatch outside those 34 entries:
the generated prompt requires only the entity argument, whereas the solution
calls aspect callbacks with `(entity, undefined)`. It is recorded separately as
E01, without changing the historical target set or counts. The new candidate
leaves that detail unspecified; the ideal reference includes it as requirement 35.
The same-analysis control retains this wrong sole-argument claim as well.

Excluding four edge-policy bundles does not change the conclusion: the previous
generated prompt has 18 fully specified bundles; the new candidate has three.
The new candidate is 547 words; the earlier generated prompt is 2,060 words.
Shorter length does not compensate for the lost semantic detail.

## What was assembled

[candidate-prompt.md](candidate-prompt.md) has all six requested sections:
Objective, Requirements, Constraints, Relevant context, Acceptance criteria,
and Verification and completion.

The builder preserves **all 25 product clauses** from the saved production
split/routing output, in order, and adds the three directly rendered decisions.
It retains the original metadata request despite unresolved mutability questions.
The two branch/commit clauses are excluded because Powdrr owns that workflow.
No original product clause is dropped in favor of an acceptance example.

Only the three resolved clarifications have concrete acceptance scenarios.
The other numbered requirements remain completion conditions. That is a visible
limitation: this is a complete-format prompt with incomplete semantic enrichment,
not acceptance scenarios for every feature.

Context comes from verified original code. Verification uses the actual core
package, `@koota/core`, and its `test`/`lint` scripts. No nonexistent `typecheck`
script is invented. These commands are text for the eventual coding agent;
they were not executed in this comparison.

[candidate-provenance.json](candidate-provenance.json) maps every numbered
requirement and scenario to a source clause or selected decision. Candidate
assembly reads no solution, oracle, reference prompt or comparison labels.

## Same-analysis control: what removing the rewrite actually changes

[same-analysis-prompt.md](same-analysis-prompt.md) retains the earlier generator's
40 analysis records as its input, rather than limiting analysis to three new
decisions. It renders the 35 records labeled instruction/repository directly;
one assumption and four unresolved proposals are withheld. Original product
clauses remain present. Selection uses saved classifications, not solution
review, and every selected rule is copied exactly.

This control removes the invented `add(aspect, data)` signature because no
selected analysis record contained it. It **retains deep freezing and nested
callback expansion**, because those wrong policies already existed in f012 and
f005 before the final rewrite. It also retains the wrong sole-argument callback
claim from g020/g021/g022.

Its coverage decreases where the old formatter promoted unresolved proposals
into obligations. Some of those proposals matched the solution (equal-value
notifications, shared-reference read results and subscription multiplicity).
Their correctness needs to be established in explicit decisions; a formatter
must not decide whether to turn them into requirements.

The control also repeats overlapping source clauses and analysis rules. Literal
rendering avoids semantic mutation but does not perform semantic deduplication.
It has a generic completion-condition section rather than freshly generated
acceptance scenarios. It is an evaluation control, not an approved final prompt.

Therefore neither the three-decision candidate nor this simple same-analysis
control is ready. The useful change is removing the final interpreter while
repairing and completing the decision records that it renders.

## Concrete improvements

| Earlier generated text | New complete prompt | Effect |
| --- | --- | --- |
| “deeply frozen schema” in requirements, constraints, context and criteria | Only non-reassignable metadata is asserted; unresolved freezes are withheld | Removes a contradiction with the solution's shallow freeze, but does not recover container protection |
| Nested constituents “each occupying its own slot” | R26 and AC1 require one merged object for the directly supplied nested aspect | Corrects the nested readEach case; broader slot rules still need analysis |
| `add(aspect, data)` in acceptance criteria | Retains the original add request without inventing a calling convention | Removes a wrong API signature, but fails to specify correct `add(aspect(data))` |
| `@powdrr/core` test/lint/typecheck commands | Actual `@koota/core` test/lint scripts | Corrects validation instructions |
| Notification answer “Yes” plus conflicting uncertainty in the last experiment | R27 and AC2 state equal-value behavior directly | Complete, stable wording of the selected behavior |

The earlier LLM-generated prompt already stated equal-value notifications
correctly. The earlier hand-written candidate restricted them to changed data.
The new wording improves over the latter and preserves the former's useful
behavior. These baselines must not be conflated.

## Detail that the new candidate loses

Sixteen bundles previously fully specified become partial or missing. The
most actionable losses are below; they are not missing original requests, but
missing behavioral decisions beyond the short original instruction.

| Behavior | Previous generated prompt | New prompt | Required in ideal/solution |
| --- | --- | --- | --- |
| Construction identity | Deduplicate, retain first occurrence, minimum two unique traits | Flatten; accept two or more inputs | Count unique flattened traits; preserve first occurrence order |
| Public type | Export associated types | Export createAspect | Export Aspect as well |
| Incomplete entity set | No-op if any constituent is missing | Distribute fields | No writes or events when incomplete |
| Unknown/omitted fields | Ignore unknown; preserve omitted | Route fields to owners | Ignore unowned fields; preserve omitted values |
| Removal of absent constituents | Explicitly tolerate | Remove all constituents | Remove present constituents; tolerate missing; preserve unrelated traits |
| Update mode | Never commits without notifications; non-never detects changes | Distribute writes | Separate committing from notifying |
| Destruction in callback | Skip subsequent writes/events | Unspecified | Check entity still exists before committing |
| Event timing | Dispatch after iteration | Unspecified | Dispatch collected notifications after callbacks finish |
| Changed membership | Require all present plus any constituent changed | Any constituent changed | Require current completeness and tracked change |
| Or grouping | Constituent alternatives | “Compose with all modifiers” | Or(aspect(A,B),C) means A OR B OR C |
| Unsubscribe | Detach all constituent hooks | Unspecified | Return function that detaches every hook |
| Aspect onChange count | Per constituent, not once per operation | Fires when any changes while complete | Two changed constituents may produce two callbacks |
| Reset | Clear old results; support new registrations | Unspecified | Aspect querying continues after reset |

Other uncertainties were present even in the previous generated prompt:
fresh shallow read results versus nested aliasing, triggerChanged=false,
tracking observation boundaries, query-cache identity, multiple Not aggregation,
mask generations and function-schema ownership. Direct rendering does not
resolve any of those by itself.

## Why the comparison matters

The new complete prompt was assembled from original requirements plus **only
three** resolved clarifications. The earlier generated prompt consumed a broader
analysis. Therefore the coverage loss cannot be attributed to removing synthesis
alone. The controlled saved-choice replay showed preservation; this full
comparison shows that the experiment's analysis coverage is inadequate.

This distinction gives a concrete course of action:

1. Keep the direct-rendering assembly principle and original-clause preservation.
2. Restore the broader analysis coverage as explicit scoped decisions; do not
   drop useful existing decisions merely to remove a rewrite call.
   Correct f012's freezing-depth decision, f005's construction/projection
   conflation, and g020/g021/g022's callback-argument claim before rendering them.
3. Start with incomplete-set handling, write/notification controls, callback
   slot mapping, Or grouping and subscription multiplicity. These determine
   observable implementations and account for several current gaps.
4. Require separate decisions for metadata assignment, container protection,
   recursive freezing and reference preservation. The last question in the
   preceding experiment still combined the last two.
5. Repeat this same full comparison before proposing production integration.

This is a recommendation from the comparison, not an implementation or a
demonstration that Flash now passes the task. No production changes or coding
runs were made.

## Baselines and reference provenance

- **Previous generated:** the saved science LLM output at
  `operation-analysis-v1/focused/prompt.md`; it is not a captured standard
  Powdrr production prompt. That capture is still unavailable for this task.
- **Earlier hand candidate:** `six-section-hard-task-v1/.../prompt.md`, kept
  separate. Its original normative patch critique is superseded; this review
  treats the solution as correct by definition.
- **Ideal:** no separately saved complete ideal text for this Koota task was
  found. The pre-existing frozen oracle supplies the ideal target decisions.
  [ideal-reference-prompt.md](ideal-reference-prompt.md) is a new offline rendering
  of those targets, plus the callback detail confirmed in the patch. It never
  feeds the candidate builder. The saved state-machine ideal is a different task.

The reference is a hindsight evaluation artifact. Coverage of its known targets
does not establish exhaustive specification of every observable patch detail
or that a coding agent will implement them reliably.

## Reproduction and verification

`build_candidate.py` deterministically constructs the candidate from recorded
production routing/splitting, selected decisions, and original package metadata.
`compare.py` independently writes the offline reference and static review.
Use the shared Python interpreter and current worktree's root/`src` imports.
The original base checkout is needed only to verify the package scripts.

There are no model calls or retries. Checks cover original-clause preservation,
exact selected wording, six-section output, quoted evidence, source/reference
hashes and credentials, plus scoped Ruff formatting/lint and mypy. No tests ran.
