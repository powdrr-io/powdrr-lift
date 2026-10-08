# Routing dataset pilot

This directory starts Milestone B from
[`routing-classifier-jev-and-dedicated-model-handoff.md`](../../../docs/plans/routing-classifier-jev-and-dedicated-model-handoff.md).
It collects original DeepSWE instruction sentences, preserves source text and
spans, prepares blinded annotations, and validates family-level splits. It does
not contain a finished gold set or a trained model.

The merged Jev request changes and their authored smoke comparison are recorded
in [jev-experiment.md](jev-experiment.md).

## Current pilot contents

- 114 original instructions across Python, Go, TypeScript, Rust, and JavaScript.
- 2,192 exact source-sentence candidates from 91 repository families.
- 1,856 development candidates. Families used by the existing Python
  disposition work are forced into development.
- 336 candidates in a reserved fresh-family candidate holdout. Labels are not
  reviewed yet; do not use these rows for prompt, model, or threshold selection.
- A deterministic 240-item, development-only review packet is generated under
  `/private/tmp` by the command below. It samples across repository families
  and uses legacy disposition suggestions plus lexical slice tags only for
  hidden sampling strata.

The old disposition labels are **silver suggestions**, not routing gold. Their
translation is deliberately incomplete: product descriptions suggest `include`,
`non_goal` suggests `include_prohibition`, `nonactionable` suggests `exclude`,
and `context` suggests `context`. Every suggestion needs human review. The
suggestion is never included in the blinded reviewer sheet or inference input.

The original source metadata does not declare a license for these task
instructions. The data records `license: unknown`; resolve the source terms
before exporting this corpus outside its current repository/work environment.
The repository is public, so `.gitignore` keeps the copied source instructions,
candidate sentences, and reviewer material local. The PR tracks the collection
tools, schemas, and non-text split/count manifests; it does not publish the
unlicensed task text.

## Build and validate candidates

Use the shared environment. `--tasks-dir` should point to the local DeepSWE
tasks checkout; it is intentionally not hard-coded into the dataset.

```bash
export VIRTUAL_ENV=/Users/gregory/code/powdrr-lift/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
export PYTHONPATH="$PWD/src"

rtk proxy "$VIRTUAL_ENV/bin/python" science/classifications/routing/build_dataset.py \
  --tasks-dir /Users/gregory/code/powdrr-deep-swe/tasks

rtk proxy "$VIRTUAL_ENV/bin/python" science/classifications/routing/validate_dataset.py
```

The builder stores exact source documents separately from candidates, checks
source spans, hashes source bytes, and assigns every repository family to one
split. Existing classifier families are development only. Other family
assignments are deterministic from the family ID; the split manifest is checked
and hashed in the tracked aggregate manifest. The family-to-split map stays
local so the fresh-family identities are not published with this pilot. Raw
documents, candidates, and the family map are written locally to ignored paths.
Rebuilding after source changes requires reviewing the new candidate hash and
regenerating the review packet in a new directory.

## Prepare and complete the human review packet

Keep the coordinator key outside any annotator handoff. Share only one
reviewer sheet with each independent reviewer; do not share the whole packet
directory or candidate data file.

```bash
rtk proxy "$VIRTUAL_ENV/bin/python" science/classifications/routing/prepare_review.py \
  --output-dir /private/tmp/routing-review-packet
```

The output includes `reviewer_a.jsonl`, `reviewer_b.jsonl`,
`review-manifest.json`, and the coordinator-only `selection_key.jsonl`. Reviewer
sheets contain only the exact target, local context, scope relations, and blank
annotation fields. The key contains the source IDs and silver suggestions. The
packet is development-only, not a fresh evaluation sample.

Use [annotation-guide.md](annotation-guide.md). A reviewed gold label requires
agreement from two distinct human reviewers. Disagreements and assistant-model
labels remain outside the gold file pending human adjudication.

After filling both sheets, finalize them into a new output directory:

```bash
rtk proxy "$VIRTUAL_ENV/bin/python" science/classifications/routing/finalize_labels.py \
  --packet-dir /private/tmp/routing-review-packet \
  --reviewer-a-kind human --reviewer-a-id reviewer-a \
  --reviewer-b-kind human --reviewer-b-id reviewer-b \
  --candidate-file science/classifications/routing/data/candidates.jsonl \
  --output-dir /private/tmp/routing-reviewed-seed
```

Only independent human agreements and human-adjudicated rows enter
`gold-labels.jsonl`. The append-only event ledger preserves both reviews.
Disagreements and non-human reviews go to `adjudication-worklist.jsonl`; they
are not silently converted to `unclear`. A third human may resolve a disputed
item with a separate JSONL file containing `review_id`, `final_label`,
`ambiguity_reason`, `rationale`, and
`adjudicator_id`, then rerun finalization into a new output directory using
`--adjudications /path/to/adjudications.jsonl`. Low-confidence agreements also
remain in the worklist. Only two distinct human reviewers plus, when needed, a
distinct human adjudicator can create a gold row.

## Inference input

`input_contract.py` defines `routing-input-v1`: only the target proposition,
source-local context, and scope relations are serialized. Labels, IDs, family,
split, slice tags, rationale, evidence, reviewer identity, and silver
suggestions are excluded. Its canonical serializer provides stable bytes for
input hashing. Scope relations are currently null in this initial source import
because the retained task documents do not contain the compiler's source
relations; do not synthesize them from clause adjacency.

Before training or reporting an apples-to-apples comparison with Jev, align this
input contract with the production request. The merged Jev adapter currently
also provides the full instruction ledger; this pilot's bounded context does
not yet serialize that additional state. The candidate set is suitable for
annotation workflow development, but not yet a claim that local and Jev models
receive identical input.

## Promotion status

This is a candidate corpus, not gold. The 240 review packet has not been
completed, and the family holdout has not been independently labeled. Do not
train or tune on the holdout. Do not report accuracy from the old Jev smoke
cases as a fresh test. The promotion-size and class-coverage gates in the
handoff remain incomplete.
