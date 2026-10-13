# Production routed acceptance criteria run

This run answers whether template matching changes when it receives the
production instruction analysis instead of the experiment's former shared
inventory. For each instruction, it ran deterministic sentence capture, the
production JEV atomicity judge, the production planning-model splitter, the
production JEV root router, then the route-aware template selector and binder.
The final prompt is in each task's `templates/prompt.md`; each routed ledger and
classification is in `instruction-analysis/analysis.json`.

The model calls used the Typesafe.ai JEV endpoint (`jev-latest`) for atomicity
and routing, and DeepSeek V4 Flash through DeepInfra for splitting and template
matching. No coding agent or task test suite was run. The report evaluates
criteria against 91 agent-authored, source-based reference validations using an
automated Qwen reviewer; neither the references nor reviewer are human gold.

## Results

| Task | Clauses before → after split | include / prohibition / context / exclude / unclear | Recall | Precision | Criteria | Match calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| cattrs partial structuring | 13 → 28 | 26 / 0 / 0 / 2 / 0 | 88.2% | 100% | 25 | 27 |
| Helm array merge strategies | 24 → 39 | 36 / 0 / 1 / 2 / 0 | 93.1% | 100% | 50 | 42 |
| Koota entity snapshot rollback | 25 → 48 | 46 / 0 / 0 / 2 / 0 | 100% | 91.9% | 74 | 50 |
| dateutil timezone interoperability | 37 → 56 | 53 / 0 / 1 / 2 / 0 | 92.0% | 92.3% | 65 | 58 |
| **All tasks** | **99 → 171** | **161 / 0 / 2 / 8 / 0** | **93.4%** | **94.9%** | **214** | **177** |

The automated reviewer fully covered 85/91 references. Six were missing or
partial; seven emitted criteria were unsupported and four were partly supported.
All four prompts completed. One quarter of tasks had every reference fully
covered. Eight binding decisions with invalid source quotes were withheld as
`unknown`, with the validation error recorded; their source requirements remain
in the generated prompt's fallback section if no other criterion represents
them. The matcher emitted criteria only for `include` clauses in this sample.

For a rough historical comparison, the previous template runs across these same
four tasks scored 89/91 strict recall (97.8%) and 231/233 strict precision
(99.1%), with two of four tasks fully covered. They emitted 233 criteria and
37,176 criterion characters. This run scored 85/91 and 203/214, emitted 214
criteria and 37,966 characters. Its matching stage used 177 calls and 2,014
recorded call-seconds; production analysis added 311 decisions (270 JEV and 41
split calls) and 174 recorded call-seconds. The prior runs used 28 matching
calls, 730 matching call-seconds, and 66 inventory call-seconds.

This is a directional comparison, not a controlled estimate of the effect of
routing and splitting: the current matcher prompt and catalog version changed,
and matching used batch size 1 to avoid incomplete large responses (the prior
run used batch size 6). Recorded call-seconds sum provider request times; the
JEV routing calls ran concurrently. Token usage was not returned by the
providers. The sample contained only two context clauses and no
`include_prohibition` or `unclear` routes, so this run does not test negative
criterion generation or abstention quality. Production's route name is
`include_prohibition`; there is no `include_negative` enum in this flow.

## Reproduce

From the repository root, with the shared environment and provider credentials
configured as in `AGENTS.md`:

```bash
export VIRTUAL_ENV=/Users/gregory/code/powdrr-lift/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
export PYTHONPATH="$PWD/src"

rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.production_analysis \
  --inputs-dir science/deepswe/acceptance_templates/data/inputs \
  --output-dir /tmp/acceptance-production-routed \
  --tasks cattrs-partial-structuring-recovery helm-array-merge-strategies \
          koota-entity-snapshot-rollback dateutil-rfc5545-timezone-interop \
  --batch-size 1 --repairs 3

rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.cli evaluate \
  --inputs-dir science/deepswe/acceptance_templates/data/inputs \
  --run-dir /tmp/acceptance-production-routed \
  --references-dir science/deepswe/acceptance_templates/data/references \
  --provider deepinfra --model Qwen/Qwen3-Next-80B-A3B-Instruct \
  --batch-size 6

rtk proxy "$VIRTUAL_ENV/bin/python" -m science.deepswe.acceptance_templates.cli score \
  --inputs-dir science/deepswe/acceptance_templates/data/inputs \
  --run-dir /tmp/acceptance-production-routed \
  --references-dir science/deepswe/acceptance_templates/data/references
```

`call-checkpoints.tar.gz` contains 567 request/response records, including the
atomicity, split, route, match, repair, and review calls. Its file hashes and
request fingerprints are in `call-checkpoints.json`. To inspect or resume those
calls, extract the archive from the repository root; raw call directories are
ignored by Git.
