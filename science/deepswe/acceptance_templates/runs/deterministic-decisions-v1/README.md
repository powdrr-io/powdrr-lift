# Direct rendering of decisions: Koota composite trait aspects

## Result

Removing prose synthesis preserves the selected decisions without the previous
invented explanation, equal-value notification hedging, or optional freeze
recommendations. The rendered fragment has three correct requirements and three
observable scenarios. Metadata remains incomplete: both models abstained on
container protection and nested freezing.

| Area | Previous synthesis | Direct rendering |
| --- | --- | --- |
| Nested aspect callback grouping | Correct choice, but falsely claimed original code already supports aspects | One merged callback object; no fabricated repository explanation |
| Equal-value setter notifications | Bare “Yes,” with uncertainty about the aspect operation | Complete statement and observable scenario; explicit aspect-behavior classification |
| Metadata | Made shallow freezing optional and permitted array mutation | Non-reassignable properties; no unsupported permission or freezing obligation; three dimensions remain unresolved |

The improvement is **preserving the decisions**, not recovering every solution
detail. This is a focused fragment, not a full six-section prompt or a coding
success result. No production pipeline, tests, or coding agent ran.

The follow-up made 12 calls: five Flash classifications and seven Typesafe.ai
classifications/reviews. Summed call durations were 7.981 seconds; parallel calls
overlap, so this is not wall-clock runtime. Token usage was unavailable. The exact
saved-choice ablation required no calls. This workload differs from the preceding
experiment, so the timing is not a controlled speedup measurement.

## Method

### 1. Exact ablation: saved choices to saved meanings

`frozen-choice-ablation.json` reuses the previous three atomic observations.
Each choice is rendered using its saved option meaning. There are no calls.
Model-generated reasons, transfer rationales and synthesized answers are omitted.

The callback observation establishes requested feature behavior. The other two
are **existing-code facts**: queryRef freezing depth and regular-trait setter
notifications. Rendering them as new aspect obligations would silently perform
the same unjustified transfer as the removed synthesis step. They remain context.

This is the controlled ablation: same decisions, no resampling or changed
questions, and no new behavior is injected into their meanings.

### 2. Explicit feature decisions

Five additional questions cover the missing setter transfer and metadata levels.
They are answered as bounded choices, not generated contract prose. Flash and
Jev see the same original-source packet and answer independently. Neither sees
the other's choice. Unknown and disagreement are retained without blocking output.

| Decision | Flash | Jev | Rendered? |
| --- | --- | --- | --- |
| Aspect equal-value notifications inherit regular set behavior | retain_set | retain_set | Yes |
| Public metadata property reassignment | protected | protected | Yes |
| Exposed traits-array structural mutation | unknown | unknown | No |
| Merged schema top-level mutation | unknown | unknown | No |
| Nested schema freezing/reference policy | unknown | unknown | No |

The original metadata requirement remains in the recorded source inputs and
`unresolved.json`; withholding added clarification does not discard that request.
This experiment renders only the focused additions. Integration into a complete
prompt must preserve the original requirements as well.

The additional questions are an exploratory arm, not the same-input ablation.
In particular, the notification improvement comes from asking about **aspect**
setter behavior explicitly, rather than applying a renderer to an existing-code
fact and pretending the inheritance decision was already made.

### 3. Frozen statements and scenarios

Every choice has a predefined meaning, requirement statement and observable
scenario. All definitions are saved and hashed in `manifest.json` before calls.
Classification payloads contain only the original sources, one question and
the alternative meanings. The model never writes the requirement or scenario.

Code selects the frozen strings for an agreed, non-unknown choice. Jev checks
scenario meaning preservation for the two newly classified rendered decisions;
both return `preserved`. The reused callback scenario is checked in the static
solution review. Scenario approval checks meaning preservation, not correctness
of the selected policy or coverage of the task.

The final fragment contains no model explanation. Code renders existing-code
observations into context without changing their scope. Every requirement and
scenario has a decision ID and selected-option provenance.

## Solution review

The solution is correct by definition. The same previously frozen target
decisions are used for review; no patch or oracle enters a classification input.

- **D16, nested callback-slot facet:** the solution assigns one mapping to the
  directly supplied aspect and merges its constituent snapshots into one
  `state[i]`. The rendered statement and scenario match. This experiment does
  not cover all of D16, such as tag-only aspects and surrounding parameter order.
- **D34, setter equality:** `setAspect` groups supplied fields by owner and calls
  existing `setTraitForTrait(..., triggerChanged)`. That function has no equality
  guard before `setChanged`. The rendered requirement and one-subscriber scenario
  match, given a complete entity and one supplied constituent field.
- **D06, metadata assignment facet:** the solution uses non-writable property
  descriptors. The assignment-protection statement and scenario match.
- **D06, remaining facets:** the solution shallow-freezes the traits array and
  merged schema and retains nested references. Those details are not resolved
  or rendered here. A correct answer about `Object.freeze(queryRef)` does not
  establish them for the new aspect.

There are no wrong rendered requirements or fabricated explanations in this
fragment under this agent-authored review. That is not a general false-positive
rate: the questions, alternatives and wording were manually designed on a known
development task. The nested-freezing alternatives also combine freezing and
reference preservation; they need separate questions in a general implementation.

## What to carry forward

- Remove the model call that rewrites selected decisions into a contract.
- Keep each question's subject/operation, alternatives and selected meaning
  together. Existing-code observations and requested behavior are different
  record types and must render into the appropriate section.
- Ask the inheritance question explicitly when moving from an existing API to
  a new operation; the renderer must not make that decision.
- Render only the selected wording. Keep rationale in review artifacts rather
  than the coding prompt.
- Preserve unresolved dimensions and the original requirement; generating a
  prompt must continue. Rendering can prevent additions and drift but cannot
  supply missing decisions.

This experiment does not establish how to automatically discover questions,
create their alternatives, or bind them from arbitrary instructions. The next
quality problem is resolving metadata invariants and transferring existing
behavior, not another formatting rewrite. No generic question-generation or
acceptance-template catalog was added here.

## Artifacts

- `run_experiment.py`: classifier calls, exact ablation, deterministic rendering,
  scenario review and checkpoint archive generation.
- `manifest.json`: frozen question/alternative/statement/scenario definitions.
- `inputs/`: allowlisted original-source classification inputs.
- `decisions/` and `results.json`: model choices, selected strings and provenance.
- `frozen-choice-ablation.json`: exact reused decisions with preserved scope.
- `prompt-fragment.md`: the directly rendered additions and scenarios.
- `unresolved.json`: missing decisions, without withholding generated output.
- `review.json` and `run-summary.json`: static target comparison and measurements.
- `call-checkpoints.tar.gz` and `call-checkpoints.json`: all new calls.

Reproduce using the shared Python environment with this worktree's root and
`src` on `PYTHONPATH`. Extract the call archive here to replay recorded calls;
without checkpoints, rerunning invokes the providers. The preceding experiment's
saved inputs and callback choices are also required. Replaying rejects changed
definitions or request fingerprints rather than silently reusing stale answers.

Verification uses scoped Ruff formatting/lint, mypy, and deterministic checks of
source packets, selected wording, checkpoint provenance and credentials. Tests
remain disabled at the user's request.
