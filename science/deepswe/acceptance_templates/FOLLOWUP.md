# Applicability and reviewer follow-up

## What changed

This follow-up addresses the unsupported identity and precedence matches, and
the reviewer mistakes found in [PILOT.md](PILOT.md). It changes the standalone
science runner. Production classifiers and prompt generation are unchanged.

1. T31 requires an object-reference relationship, T18 a conflicting-value winner,
   and T17 selection of an effective configuration value. Each card records that
   prerequisite and its near misses.
2. Accepted bindings require evidence for every prerequisite and filled slot:
   cited spans, separate quotes, and a support explanation. Local validation
   aligns whitespace/backtick differences, restores original text, and records
   absolute offsets. It never treats a quotation as proof of entailment.
3. Guarded bindings receive a separate **source relationship classification**.
   The classifier sees the instruction, requirement, and operation locator. It
   does not see the proposed criterion, proposed winner, required class, or
   binder explanation. Code maps the classified relationship to eligibility.
4. Support review sees final rendered text, not unused binding evidence. The
   optional assertion review decomposes a criterion without seeing the source,
   judges each assertion against the instruction, and aggregates verdicts in code.
5. Coverage schemas constrain reference/requirement/criterion namespaces and
   verdict counts. Changed generations preserve previous artifacts by hash;
   review calls use a namespace derived from their actual inputs and rubrics.
   Validators annotate copies, preserving the raw model responses.

Missing guard evidence or a provider failure withholds that instance; generation
continues. Alternative criteria and any otherwise unrepresented requirement
remain in the prompt. An unresolved match is not credited as a successful match.

## What the attempted fixes actually did

| Attempt | Observation | Conclusion |
| --- | --- | --- |
| Explicit prerequisites and quoted slot evidence | dateutil's read-only tuple no longer became T31 identity; Helm still bound append order to T18 | Quotes improve traceability, not entailment |
| Yes/no confirmation of the proposed criterion, Deepseek | Still accepted append order; one intermediate version also rejected the genuine user-field winner | Asking for approval repeats the original interpretation |
| Yes/no confirmation, Qwen | Still accepted append order as a winner | Changing model family alone did not solve it |
| Source-only relationship classification, Qwen, actual Helm requirements | r016 classified as `retain_both_in_order`; r003 as `select_one_source_value`; r014 as `select_effective_value` | Code rejects the append/winner match while preserving the real winner and override |
| Revised whole-criterion rubric, actual cattrs replay | Still approved the invented `detailed_validation=False` policies | Rubric reminders alone were insufficient |
| Separate assertion review, same Qwen reviewer and actual cattrs criterion | Field-map assertion supported; empty `error_map` and single summary exception assertions unsupported | Aggregation correctly marks the criterion partly supported |
| Revised coverage rubric, actual Helm warning replay | Marked full coverage when a strong criterion existed alongside a weak redundant one | This specific coverage error was corrected |

Earlier literal quotation validation sometimes withheld a wrong semantic answer
because its Markdown formatting differed. Once formatting alignment accepted
those quotations, the semantic mistake reappeared. Such format failures are
preserved and are **not** counted as a semantic fix. Confirmation prompts that
failed are preserved alongside the successful relationship classification.

## Targeted controls

[Fresh neutral-ID binding controls](runs/contrasts-neutral-fresh-v3/report.json)
passed **11/11** nominated requirement/template decisions using Deepseek for both
binding and any source relationship checks. They cover:

- Read-only and equal-content statements rejected as identity; actual reference
  sharing accepted, including a statement without the word “identity.”
- Concatenation and traversal order rejected as precedence; their ordering
  templates accepted.
- Actual conflict winners and nearest-source overrides accepted.

This checks the complete binding/guard path with nominated templates, not
whole-catalog selection recall. Eight synthetic instructions and agent-authored
labels are too small to estimate population precision or recall.

Earlier contrast runs used descriptive task IDs that could hint at the answer.
Their results are preserved as development trials, not credited as clean
calibration. The fresh run uses neutral IDs derived from instruction hashes.
[Neutral assertion controls](runs/atomic-controls-neutral-v3/report.json) passed
**2/2**, accepting the legitimate read-only tuple and shared-reference criteria.

The old and revised whole-criterion reviewer rubrics each passed **26/27** strict
status checks on the earlier small contrasts. Both called a mixed supported and
invented criterion unsupported rather than partly supported; both deny strict
precision credit. The near-identical scores do not establish reviewer progress.
The actual original-payload replay is more useful:
[rubric replay](runs/review-replay-v2/report.json) passed **1/2**, whereas
[isolated assertion replay](runs/atomic-review-v3/report.json) detected the cattrs
error (**1/1** targeted check).

## Full-task rerun

Original instructions, requirement inventories, reference labels, and source
hashes are unchanged. Selection/binding were regenerated for Helm and dateutil,
then the saved bindings were classified. No new inventory, direct generation,
coding agent, or task verifier was run.

The generation uses Deepseek; the full-task relationship check uses Qwen. The
ordinary runner uses its configured generator for relationship checks. The
neutral controls exercise that default; they do not prove identical full-task
behavior across models. Both old and new final texts were reviewed with Qwen,
the current rubric, and batch size 12. Full-task support uses whole-criterion
review, not assertion review.

| Task | Original criteria: full references; supported rows | New criteria: full references; supported rows |
| --- | --- | --- |
| Helm | 29/29; 63/66 | 29/29; 40/40 |
| dateutil | 25/25; 56/62 | 25/25; 56/65 |
| Combined | 54/54; 119/128 (93.0%) | 54/54; 96/105 (91.4%) |

Sources: [original outputs under current review](runs/evidence-control-v3/report.md)
and [new outputs](runs/evidence-typed-v3/report.md). These are automated judgments
against agent-authored references, not human gold. Criterion grouping differs,
and the reviewer has demonstrated errors. An intermediate review even marked
the same dateutil RFC-comment issue differently. Do not treat a one-row change
in these reviews as verified improvement or alter reference labels to improve a
score.

The new generation required about **1,300 seconds** of summed model-call time,
versus about **374 seconds** originally, excluding shared inventories and review.
The binding batch changed from 6 to 3; proof fields and checks added work. Token
usage was not reported by the provider and remains null. This is not an
equal-cost comparison. The combined precision did not improve.

## Remaining concrete error

Eight of the nine dateutil rows flagged in the latest review turn an output
format or positive support statement into a rejection policy through T13.
For example, requiring UTC serialization with a `Z` suffix becomes an obligation
to reject input without that suffix. The remaining row turns “adds exclusions”
into “contains exactly these exclusions,” implying replacement/exclusivity.

The next bounded experiment should distinguish input acceptance, output
serialization, and explicit rejection before T13 eligibility. Its renderer
currently requires both acceptance and rejection clauses. A source that only
states positive support must not force a fabricated negative clause; evaluate a
separate positive-only renderer or independently optional rejection clause.
Contrast controls must include real rejection policies as positive controls.
Likewise, additive updates need separation from replacement/exact membership.
Those changes are **not implemented in this follow-up**.

## Artifacts and reproduction

- [README](README.md) documents the runners and configuration.
- `runs/evidence-v3` preserves the selection/binding inputs to the final guards;
  `data/catalog-evidence-v2.json` is its exact catalog.
- `runs/evidence-typed-v3` preserves final decisions, rejected instances, prompts,
  final reviews, score reports, and older generations indexed by hash.
- Every follow-up run has a full checkpoint archive and per-file hashes. Early
  trials additionally use `artifacts.tar.gz` for parsed intermediate artifacts;
  extract from the repository root to restore their original paths.
- [Experiment index](runs/followup-index.json) identifies primary evidence and
  preliminary trials. Failures, discarded attempts, and superseded call costs
  remain available. No result has been promoted to human-reviewed status.

All **1,735 repository tests** passed, with 6 skipped; the focused science suite
has 64 cases. Ruff format/lint, both mypy scopes, workflow compilation/liveness,
and all eight deterministic workflow scenarios passed.
