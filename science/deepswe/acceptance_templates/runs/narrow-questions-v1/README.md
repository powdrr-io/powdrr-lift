# Narrow questions: Koota composite trait aspects

## Result

Concrete questions about execution and meaning recovered two of the three
target decisions. They also removed the erroneous deep-freeze interpretation,
but did not recover the target's shallow container freezing. Final prose still
contained fabricated supporting claims that Jev approved.

| Target decision | Direct Flash answer | Fact/scope decomposition | Atomic observation then synthesis |
| --- | --- | --- | --- |
| Read-only metadata, shallow-frozen traits/schema, shared nested references | Wrong: deep freezing, mislabeled explicit | Partial: avoids deep freezing; misses schema container freezing and relies on a false query-array analogy | Partial: avoids deep freezing; treats required container freezing as optional |
| One merged callback slot for a directly supplied nested aspect | Correct choice; incorrect explanation of original code | Unresolved: loses a correct reading of “merged data object” | Correct choice; incorrect explanation of original code |
| Equal-value aspect set still emits with triggerChanged=true | Unresolved, beginning with “No”; internally inconsistent evidence/basis | Aborted: malformed plan twice, including one explicit shape repair | Correct choice; incomplete wording and unnecessary uncertainty remain |

This is a development experiment on **koota-composite-trait-aspects**, one
sample per arm. It demonstrates improvements to two specific decisions, not a
finished prompt, statistical reliability, or coding success. No coding agent
or tests ran. The solution is authoritative throughout.

The experiment made 33 calls: 18 to Flash and 15 to Typesafe.ai. Summed call
durations were 157.074 seconds; parallel calls overlap, so this is not wall-clock
runtime. Token usage was unavailable. The archive includes one failed shape
repair and one malformed response initially mislabeled completed.

## Inputs and controls

- Same original instruction and manually retrieved base-code packets in every
  arm. Base commit: `9c434858b2b522002f8c5eb4a554fa8836a7cf3c`.
- DeepInfra `deepseek-ai/DeepSeek-V4-Flash-0731` and actual Typesafe.ai Jev.
  No classifier substitution is allowed. Requests and responses are archived.
- Solution and validation patches never enter model inputs. The experiment
  author knows the solution; question design and retrieval are manual. This
  is not blinded question discovery or a holdout evaluation.
- Existing production splitting/routing results from the preceding experiment
  remain available. This experiment isolates three downstream decisions and
  uses exact original clauses; it does not rerun or evaluate those classifiers.
- Direct and first decomposed arms use the same source packets and primary
  questions. The atomic arm is an explicitly recorded refinement after reading
  the first arm's failures, not an independent preplanned comparison.

## Arms

### Direct control

Flash answers the behavioral question from the original instruction and code.
It reports its answer, basis, source IDs, transfer rationale and uncertainty.
This controls for the manually improved source retrieval.

### Fact/scope decomposition

1. Extract instruction facts and existing-code observations; identify each
   observation's operation scope.
2. Select questions from scope/counterpart/transfer dimensions.
3. Jev checks the extracted facts together.
4. Flash answers the selected subquestions together, then synthesizes a decision.
5. Jev checks the decision and an independently supplied known wrong claim.
6. Generate acceptance scenarios only for a supported, resolved decision;
   Jev checks whether the scenarios add unsupported behavior.

This arm still bundled facts and subquestions. That weakened the proposed
single-decision decomposition: a broad support verdict did not establish that
each inference was correct. It also overemphasized explicitness and encouraged
Flash to abstain from the ordinary meaning of “merged data object.”

The notification plan returned one fact object instead of the requested plan
object. One explicit shape repair produced the same malformed shape. Both
responses remain archived; the arm stopped there. No repeated semantic retry
or fabricated completion is included. The initial response was incorrectly
marked completed by the permissive local validator; subsequent JSON Schema
validation caught it. This status limitation is retained in the archive and
called out here, rather than rewriting the original receipt.

### Atomic observation then synthesis

Each case starts with one concrete question and bounded competing answers:

| Case | Observation question | Flash | Independent Jev |
| --- | --- | --- | --- |
| Immutability | Does freezing queryRef also freeze its referenced parameters array? | No | No |
| Callback grouping | Does nested flattening replace merged aspect callback data with separate constituent objects? | No | No |
| Notifications | Does the original setter call setChanged after an equal-value write when triggerChanged=true? | Yes | Yes |

Jev sees the original sources and choices, without Flash's selected answer.
Flash then resolves the original behavioral question using this observation.
Jev checks the resulting decision. All three final support checks returned
`supported`, although source/target review found the defects below.

## Comparison with the authoritative solution

### Immutability: one error removed, target still incomplete

The solution patch's aspect constructor defines non-writable properties and
uses `Object.freeze([...traits])` and `Object.freeze({ ...schema })`. Those
are shallow freezes: nested objects retain their references.

The atomic observation correctly establishes that freezing a containing object
does not recursively freeze a referenced array. But that fact does not decide
which new containers should be frozen. The synthesized answer permits mutating
the public traits array and proposes freezing traits/schema only as an optional
default. It therefore still fails to specify the solution's required behavior.

The first decomposed answer falsely claimed query freezing protects the
parameters array. Jev approved it. Its generated criteria were later rejected
by Jev, which is useful interception but not reliable reasoning verification.

### Callback grouping: correct answer, invented repository explanation

The solution adds slot mappings: one array mapping per directly supplied
aspect. `createAspectAwareSnapshots` merges those constituent snapshots into
one `state[i]` object. Nested aspect construction does not add callback slots.

The atomic arm picks `merged`, matching the target. Its rationale falsely says
the **original** `getQueryStores` already flattens nested aspects. It does not:
aspect support is new. Jev nevertheless returns `supported`.

The direct control also picks the correct behavior, so this case is recovery
from the first decomposition's regression, not an improvement over direct
analysis with the same focused code packet.

### Notifications: correct inherited behavior recovered

The solution's `setAspect` groups supplied fields by owner and calls existing
`setTraitForTrait(..., triggerChanged)` for each affected constituent. The
original function executes `ctx.set(...)`, then
`triggerChanged && setChanged(...)`; it has no equality guard.

The atomic question establishes actual execution first. Synthesis correctly
inherits equal-value notifications. The answer field is only “Yes,” so it
needs its stored question to be meaningful. Its uncertainty field still
unnecessarily questions whether aspects pass the notification flag through.
The chosen behavior matches; this is not yet a complete standalone criterion.

## What this establishes about the generic process

1. **Ask concrete distinctions, not “is this explicit enough?”** For example,
   determine which guard executes, what value a callback receives, or which
   object a freeze protects. The stricter source-only phrasing in the first
   arm introduced false negatives in ordinary instruction interpretation.
2. **Separate observed behavior from transfer to the new feature.** Correctly
   understanding shallow freeze does not settle a new metadata policy.
3. **Keep decisions and evidence separate when scoring.** A correct choice can
   carry an invented justification; Jev approved this in callback grouping.
4. **Use Jev independently for bounded choices.** Here its three independent
   observation choices agreed with Flash and were correct. This is promising
   but far too small to establish accuracy. Whole-answer support checks were
   less dependable.
5. **Preserve the question and selected meaning in code.** “Yes” and “merged”
   must render using their question/option definitions, rather than another
   freeform synthesis inventing reasons or changing the decision.

The next experiment should isolate the remaining **transfer/invariant** step:
which mutations can invalidate the relationship among an aspect's constituent
list, merged schema, and field ownership? That is the missing reasoning here.
It should retain bounded observations and independent opinions, compare against
the same direct control, and include another task before making a generic
pipeline change. No production pipeline was changed in this experiment.

## Artifacts and reproduction

- `run_experiment.py`: paired direct/decomposed arms, checkpoints and one bounded
  shape repair. `--case` resumes only one case.
- `atomic_questions.py`: concrete observation arm; imports the same packet builder.
- `<case>/input.json`, `<case>/result.json`: original inputs and first-arm results.
- `set-notifications/failure.json`: the incomplete arm's retained failure.
- `atomic/results.json`: all three atomic cases and their independent opinions.
- `review.json`: agent-authored target/source judgments, separate from Jev verdicts.
- `run-summary.json`: call totals, timing, status caveats, source/script hashes.
- `call-checkpoints.tar.gz` and `call-checkpoints.json`: complete model request/
  response records, including the malformed plan and repair.

Use the shared Python environment, with this worktree's `src` and root on
`PYTHONPATH`. The base repository must exist at the path declared in the runner.
Extract the call archive at this experiment directory before resuming cached
calls. Without those checkpoints, rerunning invokes the providers again.

Verification: scoped Ruff formatting/lint, mypy, and deterministic artifact/
credential checks. Tests were not run, following the user's instruction.
