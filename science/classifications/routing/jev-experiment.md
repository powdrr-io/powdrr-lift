# Jev routing request experiment

`jev_smoke_cases.jsonl` is a small authored development set. The 32-case live
comparison includes the exact state-machine opening context sentence and the
next requirement, with the complete original instruction supplied to both
requests. The labels are single-author review, not independent human gold.

Using the same full instruction ledger and Jev revision `jev-1.13.0`, the
baseline request got 20/32 correct (62.5%) and the described request got 27/32
(84.4%). Seven predictions changed and all seven matched the authored labels.
Both state-machine targets were corrected: the opening “States lack…” sentence
routes to `context`, and “State accepts…” routes to `include`. The described
request got context 7/7, include 7/7, include_prohibition 6/6, exclude 6/6, and
unclear 1/6. This is a development diagnostic, not a production accuracy claim.

The paired reports are:

- `reports/jev-baseline-ledger-32.json`
- `reports/jev-described-ledger-32.json`

Earlier small-context experiments were not retained in the current comparison.
The remaining authored unclear cases need independent review. Jev still
over-reads some bare product statements as requirements.

Run either variant with a new output path:

```bash
export VIRTUAL_ENV=/Users/gregory/code/powdrr-lift/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
export PYTHONPATH="$PWD/src"

rtk proxy "$VIRTUAL_ENV/bin/python" science/classifications/routing/evaluate_jev_smoke.py \
  --variant baseline --allow-live \
  --output science/classifications/routing/reports/jev-baseline-replay.json

rtk proxy "$VIRTUAL_ENV/bin/python" science/classifications/routing/evaluate_jev_smoke.py \
  --variant described --allow-live \
  --output science/classifications/routing/reports/jev-described-replay.json
```

The runner requires `--allow-live`, refuses to overwrite reports, and never
sends expected labels or family IDs to Jev. Keep API credentials in the
environment and never include them in a report.
