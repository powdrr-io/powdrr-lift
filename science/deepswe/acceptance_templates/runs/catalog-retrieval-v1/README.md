# Catalog retrieval and coverage experiment

This run reuses the production-routed instruction analysis for four tasks and
compares candidate retrieval against a frozen set of 52 template cards. It
does not change or regenerate the final prompts. The evaluation has two
different catalog references:

- `reference_template_groups` are the pre-existing agent-authored template
  groups attached to each source-based validation. They were prepared before
  generated criteria were inspected, but are candidate mappings rather than
  human-reviewed ground truth.
- `catalog-audit` is a separate Qwen review of whether the full catalog can
  express each reference validation. It can propose groups of cards and
  repeated uses of one card. Its disagreements with the existing groups are
  surfaced; they are not automatically treated as confirmed catalog gaps.

Retrieval methods are embedding cosine thresholds over each linked ledger
item and each card's name, applicability, prerequisites, renderer, slot
guidance, and optional sentences; the selector's captured full-catalog LLM
shortlist; JEV's top-one choice per reference; and unions of the latter two
with thresholded embedding candidates. The report gives group-complete
candidate recall and mean candidate count. A group counts as retrieved only
when all cards in at least one group are present. These are retrieval
diagnostics, not end-to-end criterion quality scores.

## Results

The 91 reference validations already have at least one annotated template
group. The independent catalog audit marked 35 expressible and 56 `no_fit`;
all 56 `no_fit` decisions conflict with an existing reference mapping. This is
not evidence of 56 catalog holes. It shows the automated coverage audit has a
large false-negative or label-disagreement problem and needs adjudication
before its `no_fit` result can be used as a catalog-gap label. No confirmed
catalog gaps were established by this run.

| Candidate method | Complete reference-group retrieval | Mean candidates |
| --- | ---: | ---: |
| Captured full-catalog LLM shortlist | 37/91 (40.7%) | 3.7 |
| Embeddings, cosine >= 0.35 | 90/91 (98.9%) | 51.4 |
| Embeddings, cosine >= 0.45 | 54/91 (59.3%) | 35.2 |
| Embeddings, cosine >= 0.55 | 5/91 (5.5%) | 2.2 |
| JEV top one | 15/91 (16.5%) | 1.0 |
| LLM + JEV + embeddings >= 0.45 | 72/91 (79.1%) | 36.0 |
| LLM + JEV + embeddings >= 0.55 | 47/91 (51.6%) | 5.5 |

At this setting, thresholding embeddings alone has no useful recall/size
tradeoff: the high-recall thresholds retain nearly the whole catalog, while
the threshold that narrows candidates sharply loses most annotated groups.
The LLM selector produces small candidate sets, but its overlap with the
authored template groups is also low. The combined 0.55 candidate set includes
LLM and JEV suggestions as well as embedding hits, yet retrieves only about
half of annotated groups.

The frozen production matching results also permit a provisional stage
diagnosis per reference: a missing/incorrect route, no selected annotated
group, or a selected group without full criterion coverage. These labels use
the agent-authored template groups and automated coverage review, so they are
triage clues rather than definitive root causes. One dateutil validation for
the RFC comment correction was routed as `context`; it was never eligible for
matching and is recorded as an upstream route miss.

The provisional diagnosis is 53 retrieval misses, 2 binding/rendering misses,
1 upstream route miss, and 35 covered validations. The prior automated review
reported 85/91 validations fully covered by the generated criteria, while the
captured LLM selections fully contain only 37/91 annotated template groups.
That difference confirms the group annotations omit valid alternative
retrieval paths; their overlap scores must not be read as true recall.

## Limitations and next step

The reference template groups are not complete semantic ground truth: a
different card can render a correct criterion, and the matcher may combine
cards differently. The independent Qwen audit has not been human-reviewed and
its high no-fit rate directly conflicts with all existing mappings. Therefore
these numbers compare the retrievers against noisy mapping annotations and
should not drive catalog edits or a production threshold yet. Review the 56
audit conflicts, plus any unresolved or missing mapping cases, and adjudicate
whether each is (a) expressible by the catalog, (b) a genuine catalog gap, or
(c) unclear. Then recompute retrieval scores against those adjudicated labels.

The retrievers were compared offline against existing full-catalog LLM
selections and JEV/embedding suggestions; filtered candidates were not passed
through a fresh binder. There is no measurement here of how narrower candidate
sets change rendered-criteria quality, token cost, or wall time. Embedding
request token usage was not retained. The sample contains four tasks from one
development set, and no human gold labels.

Completed call requests and responses are archived in
`call-checkpoints.tar.gz`; raw call directories are git-ignored. The archive
contains 180 completed or failed calls from the audit iterations and JEV
retrieval, and its manifest records one interrupted audit call excluded from
the archive. `report.json` and each task's `retrieval-results.json` include
per-validation traceability and all scoring details.
