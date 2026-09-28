# Classification pilots

`compare_jev_other_classifiers.py` runs a small comparison between Jev and a
fresh DeepInfra DeepSeek V4 Flash output for source-level classifier decisions
other than root disposition. It samples three examples per teacher disposition
from non-held-out source families. The checked-in outputs are under
`jev-other-classifiers/`.

The reference outputs are model judgments, not human labels or gold data. The
pilot compares polarity, quantifier, requirement strength, precondition,
exception, explicit result, temporal scope, source predicate, behavior family,
and nonactionable exclusion safety. `behavior_family` uses the exact source
proposition as a proxy for the required behavior phrase, so that result needs
validation with proper behavior spans. Pairwise decisions and source span
extractors are not covered because the archived dataset lacks their inputs.

Run or resume the pilot with:

```bash
uv run python science/classifications/compare_jev_other_classifiers.py \
  --llm-provider deepinfra-cheap --resume
```

The report includes per-kind sample counts, agreement, and confusion matrices.
Small samples measure agreement only; they do not establish classifier
accuracy.
