# Semantic prompt baseline (2026-10-04)

This directory records one automated semantic-case expansion and three
prompt-only captures from the generic instruction-to-prompt pipeline. The
captures used the merged-main pipeline plus the local deterministic-variant
normalization fix at `ff7348ed`; the case corpus was seeded from revision
`4b0e0a1b812b4cdabea894c4ff04cd1d578ab41b`. The same source instructions and
benchmark solutions/verifier patches were used throughout. No coding worker
was run.

## Findings

| Task | Judge criteria supported | Weighted coverage | Prompt-only generation time | Correlated accepted JEV responses |
| --- | ---: | ---: | ---: | ---: |
| `python-statemachine-state-data-scoping` | 20/20 | 100% | 15m 24s | 389 |
| `ytt-jsonpath-query-api` | 12/12 | 100% | 16m 06s | 388 |
| `skrub-duration-encoding` | 14/14 | 100% | 17m 15s | 429 |

All three corrected evaluations report zero critical failures. This supports
the narrower conclusion that the captured prompts explicitly preserve the
rubric's checked behaviors. It does not show that a coding agent will implement
them correctly: prompt-only mode intentionally stopped before worker execution,
and judge coverage is not a substitute for running the benchmark tests.

The first skrub evaluation reported 11/12 because its rubric combined three
unrelated source claims (non-duration rejection, null propagation, and output
names) under an excerpt that did not occur in the worker prompt. The evaluator
correctly marked the evidence quote missing. The rubric now has separate,
source-anchored criteria for supported dataframe dtypes, rejection of
non-duration inputs, and the `duration()` selector. The corrected evaluation
scored 14/14. See the current rubric and the skrub report; the initial report
was retained outside this checked-in baseline package to avoid treating that
invalid comparison as a product finding.

The three captures took about 48m 45s in total and used 1,206 correlated,
accepted JEV responses. These costs are substantial for a prompt-only run. The
next useful quality signal is to run coding workers on a selected subset and
compare actual verifier outcomes with prompt rubric findings. The present data
does not justify claiming that 100% rubric coverage improves solution quality.

## Artifacts

- `prompts/<task>/implementation-prompt.md` is the exact captured
  worker-facing implementation prompt.
- `prompts/<task>/run-metadata.json` identifies the task for each capture.
- `prompts/<task>/prompt-quality-evaluation.json` contains criterion-level
  judge decisions and source-quote evidence.
- `variants/semantic-prompt-cases-v2.jsonl` contains the 72 base cases and 109
  accepted variants. `rejected-variants.json` records rejected proposals.
- `variants/variant-generation-exchanges.jsonl` preserves the 72 generator
  exchanges; `variant-review-exchanges.jsonl` preserves the 144 reviewer
  exchanges. Provider usage fields were not returned, so token usage is
  unavailable rather than zero.
- The two variant metadata files separate initial proposal generation from the
  replay-and-review pass. The first pass had zero accepted variants because
  equivalent returned kind labels were not normalized; the recorded proposal
  texts were reused after fixing normalization and paraphrase proposition
  drift, avoiding another generation pass.

The full Pier/container logs remain in the run's temporary artifact roots; this
directory keeps the exact worker prompts, evaluator reports, semantic case
corpus, and model exchanges needed to reproduce the conclusions without
checking in bulky execution traces.
