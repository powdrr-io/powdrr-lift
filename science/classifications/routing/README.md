# Jev routing request experiment

`jev_smoke_cases.jsonl` is a small, authored development set for the boundary
between context and product requirements, plus the other three routing outcomes.
It has six or seven examples per route. It includes the exact opening context
sentence from the state machine instruction and its immediately following
requirement, with the complete source instruction in `jev_smoke_documents.json`.
The labels are single-author review, not
independent human gold, and the examples are not representative of production
traffic. Use the run to inspect prompt wiring and find counterexamples; do not
use it to claim production accuracy or tune a confidence threshold.

The current comparable baseline and candidate reports each use 32 cases, the
same complete instruction ledger, and Jev revision `jev-1.13.0`. The baseline
recreates the prior empty-choice-descriptions request shape; it got 20/32 correct
(62.5%). The described request got 27/32 (84.4%). Seven predictions changed;
all seven matched the authored labels. Both target sentences are now routed as
intended: the opening present-state “States lack…” sentence as `context`, and
“State accepts…” as `include`. The candidate got context 7/7, include 7/7,
include_prohibition 6/6, exclude 6/6, and unclear 1/6. Jev still over-reads bare
product statements as requirements when the source itself is ambiguous.

Earlier smoke reports without the complete instruction ledger are retained as
development diagnostics, but do not use them for the before/after claim above.

Most smoke inputs use one sentence per row and a short local context label. The
two state-machine cases instead use the complete original instruction. This is
useful for a focused comparison but still too small and narrow for a quality
benchmark. The unclear cases need independent review before they can serve as
one.

This small authored comparison shows the described choice criteria and structured
instruction state improved these cases, including the motivating pair. It does
not show generalization across real instruction documents, and it does not
qualify Jev for automatic production acceptance by itself.

Run either variant with an explicit new output path. The `baseline` mode recreates
the previous empty-choice-descriptions request shape; `described` uses the current
production compiler and Jev adapter request.

```bash
export VIRTUAL_ENV=/Users/gregory/code/powdrr-lift/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
export PYTHONPATH="$PWD/src"

rtk proxy "$VIRTUAL_ENV/bin/python" science/classifications/routing/evaluate_jev_smoke.py \
  --variant baseline --allow-live \
  --output science/classifications/routing/reports/jev-baseline-ledger-replay.json

rtk proxy "$VIRTUAL_ENV/bin/python" science/classifications/routing/evaluate_jev_smoke.py \
  --variant described --allow-live \
  --output science/classifications/routing/reports/jev-described-ledger-replay.json
```

The runner refuses to call the provider without `--allow-live`, refuses to
overwrite reports, and never sends expected labels or family IDs to Jev. Reports
record per-case probabilities, returned model revision, token use, and errors.
Keep API credentials in the environment and never include them in a report.

Next work: collect independently reviewed natural instruction cases across more
project families, adjudicate boundary disagreements, then reevaluate Jev on a
fresh holdout before selecting any confidence threshold.
