# Backward prompt example: state data in python-statemachine

Status: exploratory worked example. It establishes a source-supported target
prompt and diagnoses an existing capture. It does not establish that the target
prompt improves coding outcomes or that the capture represents current `main`.

## Question

Can the worker prompt express the behavior demonstrated by the task's reference
solution, and can each part of that prompt be traced back to meaning the
instruction pipeline should recover?

This example starts with the reference behavior, builds a compact target in the
existing request format, and then compares that target with an already captured
worker prompt. The task solution, verifier patch, and rubric were inspected only
for this after-the-fact analysis. They were not inputs to the captured prompt.

## Frozen evidence and limitations

| Input | Revision or fingerprint |
| --- | --- |
| Task | `python-statemachine-state-data-scoping`, repository base `8d17ba9f6ba8420cf05fddb94013bc221ed9a222` |
| Instruction | 1,957 bytes, SHA-256 `17e0781f9837bebd7304d5f1594dbf7de9a147a3ba8f03111e005f8376b3126a` |
| Reference solution patch | SHA-256 `4197380535a2e67258afbdac3a7cb91d6a2c9ab1cd70c4cc09e973b739e4f81d` |
| Verifier patch | SHA-256 `fcf7628db832d77da0f31fb287d5f016e27c02550ca54aa6fd19c9aa4d8bf0ec` |
| Existing task rubric | `docs/evaluations/deepswe-python-statemachine-state-data.yaml`, 19 product behaviors plus a delivery-instruction absence criterion |
| Captured prompt | 52,726 bytes, SHA-256 `a6c7e2848edeb29eeb7c682c944840ee8407e3de2e96213a2ce8ab5440744202` |
| Reference content draft | 3,241 bytes, SHA-256 `a90e5d3fd2597aaf102e1a9da7caab0be8e0c17dd889a6a1f5da68b3b73c8993` |
| Capture revision | Powdrr `ef8d51d1e8e30de733b1eae17706424e882abc33`; the checked-out `origin/main` for this analysis is `dafca23c` |

The capture is a prompt-only run. Its metadata names the task `harbor-task`,
while the task metadata names it `python-statemachine-state-data-scoping`.
The existing prompt evaluator requires those identities to agree, so this
artifact cannot be reported as a valid pass from that evaluator without fixing
and rerunning the capture. A separate coding-worker trial was cancelled before
it produced a verifier result. Consequently, this example can assess evidence
and representation, but cannot say whether the target prompt helps a worker.

After this capture, `main` added compiler-owned obligation evidence expectations
to the rendered implementation packet: each actionable obligation can carry a
normative strength, diff expectation, and review routes. The current `main`
revision is `dafca23c`; the capture and reference content draft predate that
change. The 3,241-byte draft uses the same top-level product/change/validation
sections, but it is not a complete request rendered by the latest code. Its
rubric score below compares behavioral content only. A current replay must
include and measure the new evidence-expectation section before calling the
reference format-equivalent.

Building this example also exposed a formatting bug in that new renderer: it
joined evidence rows using the two-character backslash-n sequence, collapsing
them into one line. This change corrects the join and adds a regression check
that both rows appear on separate lines in the rendered packet. The reference
prompt still needs a full current-main render and evaluator run after this fix.

The existing gold rubric is a useful starting point, not independent
adjudication: its criteria were curated using solution symbols and verifier
tests. The test patch may itself encode expectations absent from the instruction.
The source-support check below excludes any requirement that cannot be justified
from the instruction and permitted base-repository context.

## Recovered behavior and source-to-prompt map

The instruction contains 32 ledger clauses in its captured form. Clause 1 is
problem context: states currently lack built-in data ownership, which motivates
the request. It is not a request to preserve the deficiency. Clauses 2 onward
specify the behavior to add. The closing branch and commit instruction is
workflow context, not product behavior.

| Requirement group | Instruction meaning | Independent solution/verifier evidence | Target prompt location |
| --- | --- | --- | --- |
| Declaration and lifecycle | Clauses 2–6: `State` accepts string-keyed defaults; each entry gets fresh data; exit clears it; re-entry resets defaults; data belongs to each machine instance. | `test_state_with_data_initializes_on_entry`, `test_data_cleaned_up_after_exit`, `test_reenter_state_reinitializes_data`, and `test_state_data_modified_in_callback_persists_within_state`; solution changes the state and machine data lifecycle. | Product contract; validation cases for default freshness, exit, re-entry, and two-machine isolation. |
| Defaults and public types | Clauses 7–12: `DataVar` supports default or factory and optional type checking; plain callables are factories; `DataVar` and `DataChangeInfo` are public imports. | `test_datavar_with_default_and_type`, `test_datavar_factory_creates_fresh_list_on_each_entry`, `test_callable_default_creates_fresh_instance_on_each_entry`, and public-import use in verifier tests; solution adds exports and data types. | Product contract; validation cases for default/factory behavior, fresh values, type enforcement, and public imports. |
| Scope and callbacks | Clauses 13–16: child callback scope combines ancestors with child override; parallel regions isolate data; callbacks receive `state_data` alongside existing arguments; callback mutations persist while the state is active. | `test_child_inherits_parent_compound_data`, `test_child_data_shadows_parent_data`, `test_parallel_regions_have_isolated_data`, `test_callback_in_child_sees_merged_data`, `test_state_data_accessible_via_callback_parameter`, and callback persistence tests. | Product contract; validation cases for merged view versus owned data, collision precedence, parallel isolation, callback signature, and mutation persistence. |
| History and queries | Clauses 17–19: deep history restores descendant data, shallow history restores direct-child data; `get_state_data` returns active data or `None`; `state_data_values` snapshots active values by state ID. | `test_deep_history_restores_data`, `test_shallow_history_restores_data`, active/inactive query tests, `test_empty_dict_data_is_valid`, and `test_state_without_data_backward_compat`. | Product contract; validation cases that keep deep and shallow history distinct, and distinguish absent data (`None`) from declared empty data (`{}`). |
| Mutation and change records | Clauses 20–25: mutation checks active state, declared key, and `DataVar` type; invalid mutation raises `InvalidDefinition`; change records contain state ID/key/old/new values and clear at macrostep boundaries. | `test_set_state_data_updates_value`, inactive and undeclared-key rejection tests, `test_datavar_type_validation_on_set_state_data`, `test_get_data_changes_returns_changes_after_set`, and `test_changes_cleared_between_macrosteps`. | Product contract; validation cases for each independent invalid condition, record fields, accumulation, and reset boundary. |
| Invalid declarations | Clauses 26–28: data must be a dictionary with string keys; `DataVar` cannot have both default and factory; invalid declarations raise `InvalidDefinition`. | `test_non_dict_data_raises_invalid_definition`, `test_non_string_keys_raise_invalid_definition`, and `test_datavar_default_and_factory_raises_invalid_definition`. | Product contract; validation cases preserve each rejected input and the specified exception. |
| Compatibility surfaces | Clauses 29–32: data survives pickling; compound and parallel states accept declarations; SCXML data is parsed as Python literals; diagrams show declared variables. | Pickle, compound/parallel, and SCXML cases exist in `test.patch`; no diagram verifier test is listed in the rubric. The solution changes serialization, state metaclass, SCXML processing, and diagram model/renderers. | Product contract; validation cases for round-trip, both state kinds, SCXML declarations, and diagram output. |
| Delivery instruction | Final clause requests a new branch and commit. | No product symbols or verifier cases; this is an execution instruction for the task runner. | Omit from product contract and behavior checks. |

The source-to-prompt relationship is many-to-many. For instance, callback
visibility depends on both callback injection (clause 15) and the scope rules
(clauses 13–14); a single test can also exercise several source obligations.
The table therefore groups requirements by observable contract while retaining
source clause identity. It does not equate one clause, one decision, one test,
and one prompt bullet.

## Target prompt in the production format

The captured request presents a product contract, required product changes, and
a validation contract. The following is a reference content draft for those
same sections. It describes outcomes and discriminating cases without requiring
the reference solution's internal classes or file layout. It has not been
compiled into a current `ImplementationRequest` and omits the optional
compiler-owned evidence-expectation section described above.

```text
Product contract:
Add per-state data to python-statemachine. State declarations accept a mapping from string keys to defaults. Each machine instance owns its active values; entering a state creates fresh values, leaving clears active values, and re-entry restores defaults. Plain callable defaults and DataVar factories produce fresh values. DataVar may enforce a value type and cannot specify both a default and a factory.

State data is available to entry and exit callbacks as state_data, together with their existing arguments. Child callback scope includes ancestor data; child values win on key collisions. Parallel regions remain isolated. Callback mutations persist while their state remains active. Deep and shallow history restore the data for the configurations each history kind restores.

Expose get_state_data(state), state_data_values, set_state_data(...), and get_data_changes(). Queries return each state's own data, not the merged callback view, and distinguish inactive/undeclared data (None) from an explicitly empty declaration ({}). Mutations require an active state, a declared key, and a value accepted by DataVar; violations raise InvalidDefinition. Change records expose state_id, key, old_value, and new_value, accumulate during a macrostep, and clear at its boundary. Export DataVar and DataChangeInfo from statemachine. Preserve active values across pickle. Support data declarations on compound and parallel states, Python literal parsing of SCXML datamodel/data id and expr values, and data-variable annotations in diagrams.

Required product changes:
Implement the public behavior above while preserving existing state-machine
behavior when data is not declared. Use repository conventions and the
existing state, callback, history, SCXML, serialization, and diagram paths.
Choose internal structure from the repository; these behavior requirements do
not prescribe a particular storage class or helper layout.

Validation contract:
Add focused tests proving:
- defaults are fresh per entry and per instance; exit removes active data and re-entry restores defaults;
- plain callable and DataVar factories create fresh values; DataVar type enforcement and public imports work;
- callbacks receive state_data with existing arguments, child data overrides ancestor keys, parallel regions remain isolated, and callback mutations persist during the active state;
- queries distinguish each state's own data from merged callback scope, active/inactive/undeclared data, and explicitly empty data;
- deep history restores descendants while shallow history restores direct children;
- set_state_data accepts valid updates and rejects inactive states, undeclared keys, and wrong types with InvalidDefinition;
- data declaration validation rejects non-dictionaries, non-string keys, and DataVar with both default and factory using InvalidDefinition;
- get_data_changes reports the specified fields and clears at macrostep end;
- pickle round-trips data; compound and parallel declarations work; SCXML data values parse as Python literals; and diagrams show declared variable names.

Keep the required assertions intact. Run focused tests and relevant existing
tests for sync/async behavior and the touched integrations.
```

This draft preserves the required worker-facing format and the task's observable
contracts. The request schema also needs repository paths, commands, and context
references from the normal bootstrap. Those values must come from the base tree
and request compiler; they are deliberately not guessed from the solution patch.
The added compatibility sentence is supported by the task's explicit request to
add data behavior and the existing no-data behavior in the base repository; it
should be verified against that base before accepting it as a gold requirement.

## What the existing prompt shows

The captured prompt has the three target sections, but expands to 52,726 bytes.
It copies the entire instruction into “Product contract,” projects many
autogenerated design sentences into “Required product changes,” then renders
32 scenarios plus relationships, default assumptions, repository evidence, and
worker policy into the “Validation contract.” The major behaviors remain
present, so raw keyword or clause coverage would overstate its quality.

Specific discrepancies visible in the capture:

1. The problem statement in clause 1 becomes a positive scenario: the worker is
   told to implement/test that states lack built-in data ownership. This turns
   the pre-existing defect into a product obligation and invites an incorrect
   negative test. Its generated scenario adds unsupported details such as
   “no error is raised,” “unsupported,” and behavior “persists for the lifetime
   of the state.” These are not specified by the instruction.
2. The request projects atomic fragments into standalone changes, including
   `data data is removed`, `get_data_changes() get_data_changes().`, and
   `state_data_values property snapshots snapshots ...`. These phrases preserve
   some source words while damaging their meaning and add redundant change IDs.
3. The prompt repeats requirements in the full source summary, change list,
   behavior scenarios, and relationships. Repetition makes it harder for the
   worker to distinguish independent contracts, cross-clause refinements, and
   implementation guidance.
4. Some scenario descriptions merge dependent behaviors (for example, pickle
   survival references nearly every feature) while the change list splits other
   single APIs into artificial additions. The target needs cross-links for
   genuine interactions, not blanket duplication.
5. Delivery instructions are present in the source summary, while the gold
   criterion correctly requires them to be absent from the product prompt.
   The captured prompt should be checked for actual leakage separately from
   its source-context section; mentioning an instruction as context is not
   itself a product requirement.

The existing evaluator scored this captured prompt **20/20 supported, 100%
weighted coverage, zero critical failures** across the rubric. That score is
correct for positive coverage: all 19 expected behaviors are mentioned and the
delivery instruction is absent from product work. It does not contradict the
findings above. The rubric has no general check for unsupported added behavior,
invented assumptions, cross-section duplication, or prompt length. The evaluator
therefore certifies presence of expected claims, while this worked example also
needs a negative/precision review. High recall on the rubric is not a sufficient
prompt-quality score.

The reference draft also scored **20/20 supported** on the same rubric after
keeping cited sentences on single lines. At 3,241 bytes, the draft is 93.9%
smaller than the captured prompt while receiving the same positive coverage
score. This is a useful result about rubric discrimination: it confirms the
draft says the rubric's expected behaviors, but does not establish equivalent
worker outcomes, less ambiguity in use, or fewer unsupported requirements.

The first evaluation of the line-wrapped reference scored 3/20, with 17 findings
changed to `ambiguous` because the judge normalized line breaks into spaces when
quoting and the evaluator checked quote inclusion as a literal byte substring.
Keeping sentences unwrapped produced a 20/20 evaluation without changing their
meaning. Evaluator evidence matching is therefore sensitive to formatting;
normalizing whitespace for evidence checks would preserve auditability while
avoiding this false ambiguity.

These are findings about the pinned capture at revision `ef8d51d`. Since it
predates current `main`, they are hypotheses to replay, not claims about current
behavior. The precise first loss is clause 1 routing: a contrast between
“current defect” and “requested behavior” was not preserved through the
instruction ledger, disposition, scenario generation, and prompt projection.
The nonsensical change phrases are later evidence of a separate atomicity and
rendering loss. Treat them as separate findings until replay shows whether one
upstream error causes both.

## What this example establishes

- **Target format:** The three user-facing request sections can state this
  task's 19 product behaviors in a more compact, source-traceable contract. The
  latest renderer also supports compiler-owned evidence expectations, which
  this draft does not yet include.
- **Interpretation distinctions:** Preserve problem statement versus requested
  behavior; avoid inventing error/unsupported behavior from missing details;
  maintain lifecycle, scope, history depth, query absence versus empty values,
  macrostep boundaries, and cross-clause dependencies.
- **Evidence loop:** Use the rubric's solution symbols and verifier tests to
  challenge the reference behavior list after deriving obligations from the
  instruction. Uncovered tests and unsupported test-only expectations remain
  explicit disagreements.
- **Not yet established:** The captured output cannot pass the current evaluator
  identity check, comes from an older revision, and has no completed worker
  score. There is no causal evidence yet that the compact target produces a
  better patch or that an intervention in the current compiler fixes the loss.
  The reference content draft has not been rendered by the latest request
  compiler, so its 20/20 result does not qualify it as a production-format
  request yet.

The evaluator was run against an evaluation-only copy of the prompt capture. Its
run metadata task ID was normalized from `harbor-task` to the task's ID after
checking that the ledger source text exactly matches the 1,957-byte task
instruction. The original task ID is retained in that copy's metadata. The prompt
bytes, prompt index, task, rubric, solution patch, verifier patch, and ledger
were unchanged. The judge used was `deepinfra-cheap`; the resulting report and
request metadata remain under `/private/tmp/statemachine-backward-example-eval/`
and are not checked into this repository. The reference evaluation used the same
judge and criteria against the 3,241-byte reference draft, with artifacts under
`/private/tmp/statemachine-backward-reference-eval/`.

## Next experiment

1. Rerun prompt capture from current `main` on the frozen instruction and base
   commit. Ensure run metadata records `python-statemachine-state-data-scoping`
   so the existing evaluator accepts the task/run pair. Save the exact request,
   prompt, ledger, bootstrap, evidence-expectation contracts, compiler/model
   revisions, and timing/cost data.
2. Run the existing prompt evaluator with this task rubric. Review every rubric
   criterion against exact prompt evidence; separately check that problem
   context and delivery instructions have not become product obligations.
3. Compile the reference draft above through the production request schema and
   renderer, including the current evidence-expectation section. If a field
   cannot express a justified requirement, record the missing information and
   the exact resulting prompt loss. Evaluate that
   reference prompt with the same rubric, then add a precision pass that checks
   for unsupported obligations and assumptions; positive coverage alone will
   not distinguish the compact reference from the captured prompt.
4. Compare current and reference prompts under one frozen worker envelope on
   the reference task. The previously cancelled worker attempt is not a result.
   Score verifier behavior and supported instruction requirements separately;
   inspect a patch only after freezing each attempt.
5. If the reference wins, replay the current instruction pipeline and identify
   the earliest decision that causes the measured prompt/outcome gap. If it
   does not, inspect prompt length, task setup, renderer, and worker variance
   before adding classifier categories.

This one-task example validates the artifact shape and exposes at least two
candidate language/representation failures. It is a worked diagnosis, not a
benchmark result or evidence of general improvement. Expand to the remaining
task families only after the current-revision capture and worker comparison are
valid and reproducible.
