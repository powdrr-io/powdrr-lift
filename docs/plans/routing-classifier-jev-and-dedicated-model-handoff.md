# Handoff: repair Jev routing, then train a dedicated routing model

## Objective and execution order

Make the instruction router reliably distinguish background context from product
requirements across unfamiliar projects. The state-machine introduction is a
development example of this distinction. Do not implement a task-name, sentence
position, or exact-text exception.

Execute in this order:

1. Repair the Jev routing request and evaluate actual predictions.
2. Build a reviewed, versioned routing dataset using the same input contract.
3. Train and evaluate a dedicated routing classifier.
4. Enable the dedicated model only after it passes the deployment gates.

This document is an implementation plan. Adding it does not implement or validate
any of those phases. Finish each milestone with its code, reproducible evidence,
and a reviewable PR. Do not wait for dedicated-model training to deliver the Jev
improvement. If a candidate fails its gate, finish the report and diagnosis; do
not claim the classifier is fixed or silently lower the gate.

The user’s useful starting signal is grammatical: background often describes
what something **is**, while requirements often use **must, should, will**, or
directives such as **start**. Teach and evaluate this signal, including its
exceptions. The desired distinction is existing circumstances versus desired
product behavior, not the presence of one token.

## 1. Repository setup and existing work

Initially inspected on 2026-10-07 at base commit `3176de06`, in worktree
`/Users/gregory/.codex/worktrees/ca0b/powdrr-lift`, branch
`feature/root-disposition-context-cases`. Refreshed against main at `17d94cba`
before publishing this plan. That newer revision already adds bounded neighbors,
removes obligation-producing uncertainty defaults, and adds a fail-closed mode
for some Jev clients. Preserve those changes. Reinspect before editing; do not
assume the working tree is clean or that line numbers remain unchanged.

Read `AGENTS.md` and `/Users/gregory/.codex/RTK.md`. Work in a feature worktree,
use the shared environment, open PRs, and leave merging to the user. Prefix shell
commands with `rtk`. Set up the shell as follows:

```bash
export VIRTUAL_ENV=/Users/gregory/code/powdrr-lift/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
export PYTHONPATH="$PWD/src"
export UV_PROJECT_ENVIRONMENT="$VIRTUAL_ENV"
export UV_NO_SYNC=1
rtk git status --short --branch
```

Do not create a worktree-local environment or run `uv sync`. If a new optional
training dependency is needed, declare it in `classifier-training` and install
only that dependency into the shared environment using the repository’s approved
workflow. Keep training libraries out of the ordinary runtime dependency set.

Existing uncommitted work at inspection:

- Grammar guidance and extra stored examples in
  `src/powdrr_lift/workrr/semantic_contract_compiler.py`.
- A test in `tests/test_semantic_contract_compiler.py` that checks those stored
  examples. This is not a model accuracy test.
- Experimental scripts/data under `science/classifications/context_detector/`
  and additions to `science/classifications/root_disposition/README.md`.
- Untracked `minilm-context-detector-v5` and `minilm-context-pairwise-v2` artifact
  directories under `science/classifications/root_disposition/artifacts/`.

Preserve and review this work. Stage named files, never `git add .`. Do not commit
the failed models’ large weight files as ordinary Git blobs. Their reports are
useful diagnostics; their weights are not production artifacts.

### Read these files first

| File | What to inspect |
| --- | --- |
| `src/powdrr_lift/workrr/semantic_contract_compiler.py` | `ClassifierDefinition`, routing definitions, root request preparation, `_classifier_request`, context extraction, decision binding and benchmark defaults. |
| `src/powdrr_lift/workrr/jev_classifier.py` | `_classifier_request`, `_call_jev`, `_format_classifier_result`, `complete_json`, and fallback behavior. |
| `src/powdrr_lift/core/semantic_decision.py` | Routing labels, result envelopes, provider metadata, input fingerprints, revision validation. |
| `src/powdrr_lift/core/classifier_input.py` | Stable target/context serialization. |
| `src/powdrr_lift/core/instruction_context.py` | Existing helper for containing and adjacent source context. |
| `src/powdrr_lift/core/instruction_ledger.py` | Original spans and atomic clause derivation. |
| `src/powdrr_lift/workrr/command_catalog.py` | Runtime root request preparation, source-text plumbing, and context retention. |
| `docs/procedrr/skill-definitions/design-interview.yaml` | Jev-backed root decision invocation and dependent branches. |
| `tests/test_jev_classifier.py`, `tests/test_semantic_contract_compiler.py`, `tests/test_semantic_decision.py`, `tests/test_root_disposition_context_retry.py` | Existing adapter, branching, revision, and source-context tests. |
| `science/classifications/root_disposition/build_dataset.py`, `compare_jev.py`, `train_classifier.py` | Reusable collection and evaluation machinery, with the limitations below. |
| `science/classifications/root_disposition/adjudication/README.md` | Actual reviewer provenance and incomplete gold status. |
| `docs/plans/local-semantic-classifier-implementation-plan.md` | Broader local-model architecture; this handoff implements only routing. |

## 2. Findings that the implementation must account for

1. **Stored examples are not transmitted.** The compiler’s `_classifier_request`
   currently serializes the question, instructions, allowed labels, and subject.
   It omits `ClassifierDefinition.examples`. Adding examples to that dataclass
   instance alone does not teach Jev anything.
2. **Jev receives empty descriptions for ordinary choices.** `_call_jev` builds
   `{label: None}` and describes only the extra `unresolved` choice. Routing
   definitions are present in general instructions, so this is an experiment in
   clearer presentation, not proof that missing descriptions caused every error.
3. **Source context is duplicated.** The adapter places subject text, a full
   request, and the outer workflow context in `state`. Trace which fields are
   needed before simplifying the routing path. Other decision kinds use this
   adapter too.
4. **Confidence is not used for acceptance.** Any valid choice is accepted by
   `complete_json`. Transport/mapping exceptions fall back to the planning model.
   An `unresolved` choice for a semantic decision returns an unresolved envelope;
   it does not currently invoke fallback. The existing abstention test’s name is
   misleading: its assertion expects zero fallback calls. Main also has
   `fail_closed=True` clients; preserve their no-planning-fallback semantics.
5. **Source context has changed since the first experiments.** Main now uses
   `_bounded_source_context` for containing and adjacent source sentences, includes
   that text in the decision fingerprint, and has neighbor-context tests. Extend
   this path rather than adding a second implementation. It still needs comparison
   with experimental serialization and checks for headings, character/token bounds,
   split clauses, and punctuation. Training and deployment must agree.
6. **Historical data is not one consistent routing dataset.** Checked-in rows in
   `root_disposition/data/root_disposition.jsonl` include older `disposition`
   labels, while the current builder prepares `routing` requests. Inspect each
   row’s decision kind and revision; do not infer its taxonomy from its directory.
7. **Earlier experiments failed.** The binary v5 detector routed all 317 source
   test cases to context at its selected threshold: 14 true positives, 303 false
   positives. Pairwise v2 used 102 training pairs and four validation pairs; it
   scored 10/14 on source test pairs and 9/15 on authored challenge pairs. Pairwise
   accuracy is not independent sentence-routing accuracy. Keep both models gated.
8. **Prior human-review claims require care.** The adjudication README identifies
   annotator A as human and annotator B as an assistant-model review. Inspect the
   actual adjudication records. Two model answers or a human/model agreement do
   not establish a completed, independently human-adjudicated gold set.
9. **Main preserves uncertainty instead of inventing obligations.** The binder
   converts a routing `unclear` prediction to `status=unresolved` with
   `reason_code=source_ambiguous`; contract compilation rejects unresolved decisions.
   Benchmark defaults no longer resolve unknown routing to include. Preserve this
   behavior and test it when adding confidence-based fallback.

## 3. Shared semantic contract

Use the existing five routing values. Do not retrain the eight-way disposition
classifier and call that a routing replacement.

| Route | Definition |
| --- | --- |
| `context` | Describes existing circumstances, motivation, or a limitation without itself specifying desired product behavior. Preserve it as supporting context, without creating an obligation. |
| `include` | Specifies positive behavior, a product definition, interface, invariant, or implementation guidance. Includes requirements written as ordinary declarative sentences. |
| `include_prohibition` | Explicitly prohibits product behavior or puts it outside the requested scope. |
| `exclude` | Process/delivery instructions or unrelated text that has no product semantics. |
| `unclear` | The supplied source supports no defensible semantic route, including irreducible ambiguity after normal atomic splitting. |

Keep semantic `unclear` separate from provider uncertainty in dataset labels and
raw predictions: the former describes the input, the latter means the provider
could not reliably answer. A trained model may predict `unclear`; a low-confidence
prediction of any label abstains. At the compiler boundary, preserve main’s
normalization of semantic `unclear` into an unresolved decision with
`source_ambiguous`. Provider abstention uses `classifier_abstained`. Neither may
be compiled into an obligation. Do not change this contract to retain a resolved
unclear route downstream.

The following are development contrasts, not unseen evaluation examples:

| Target and relevant context | Expected route | Reason |
| --- | --- | --- |
| “The cache is currently shared by every worker.” Under “Current behavior.” | `context` | Reports the present arrangement. |
| “The cache must be shared by every worker.” | `include` | Requires an arrangement. |
| “The exporter should preserve column order.” | `include` | Product guidance remains included. |
| “The exporter will preserve column order.” Under “Required behavior.” | `include` | Specifies future product behavior. |
| “Start each session with an empty cache.” | `include` | Product directive. |
| “On exit, the data is removed.” Under “Required lifecycle.” | `include` | Passive requirement despite “is.” |
| “A session is a container for one user’s state.” Under “Definitions.” | `include` | Defines a product concept. |
| “The parser currently cannot retain offsets.” | `context` | Existing deficiency, not a prohibition. |
| “The parser must not discard offsets.” | `include_prohibition` | Explicit product prohibition. |
| “Start by running the unit tests.” | `exclude` | Process directive despite “start.” |
| “The release will be reviewed on Friday.” | `exclude` | Delivery process despite “will.” |
| “The cache is shared.” With no disambiguating context. | `unclear` | Existing fact versus intended invariant is unresolved. |

Annotation rubric: determine the target’s role in its supplied passage, identify
what it requires or reports, then choose the route. Neighboring instructions can
resolve references; they cannot donate obligations to the target. Classify mixed
sentences only after the existing atomicity stage has split independent meanings.
Keep genuinely unsplittable cases as `unclear` with a reason.

## 4. Milestone A: fix Jev request construction and measure it

### A1. Establish a small routing evaluation before changing the adapter

Create `science/classifications/routing/` with `README.md`, `annotation-guide.md`,
`cases.schema.json`, `data/jev-development.jsonl`, `data/jev-regression.jsonl`,
`evaluate.py`, and `reports/`. These are proposed new files.

Start with 120 cases: 40 context, 40 include, 15 include_prohibition, 15 exclude,
and 10 unclear. Cover at least six product domains and the contrasts in section
3. Use roughly half naturally occurring instructions and half authored contrasts.
Keep variants of each base case together. Divide by family into 80 development
and 40 regression cases; report actual class counts after grouping. Use development
to revise instructions and criteria. Inspect the regression set only after the
candidate is frozen. If it later informs changes, mark it as development and
collect a fresh regression batch.

These 120 cases are a seed evaluation, not sufficient evidence for autonomous
production acceptance. Record whether each expected label is proposed, reviewed,
or adjudicated. Include the state-machine opening and its following requirements
in development. All examples already inspected in this conversation or earlier
reports belong to development, not the future fresh holdout.

The original target is “States lack built-in data ownership, forcing manual
variable management without scoping or lifecycle.” Its expected route is context.
Recover the exact passage from
`~/code/powdrr-deep-swe/tasks/python-statemachine-state-data-scoping/instruction.md`
or the retained source ledger, and preserve its hash. Include neighboring positive
requirements as separate targets. This is one development family, not the dataset.

The runner must call the same request builder and adapter used by production.
Add an opt-in live-provider mode; ordinary pytest must not call a paid service.
Implement a dry-run mode that writes the exact redacted request bodies and exits.
Keep gold labels, annotation notes, task identifiers, and expected routes out of
model inputs. Record input, prompt, rubric, code, and model revisions with output.

Capture the current request as the baseline before rewriting it. Save the returned
model identifier; `jev-latest` is mutable. Support an explicit model identifier in
the adapter/evaluator and pin it where supported. If the provider cannot honor a
pin, record the returned revision and rerun comparisons when it changes.
The current `_call_jev` returns only the answer object, discarding top-level model
and usage metadata. Preserve that metadata in an internal response record or
telemetry channel without adding fields to the workflow's strict result schema.

### A2. Define criteria once and send them to Jev

1. Add an optional per-label criteria field to `ClassifierDefinition`, with an
   empty default so unrelated definitions remain valid. Populate it for routing
   only. Use the definitions from section 3 as the initial descriptions.
2. Add explicit serialized `criteria` to compiler requests when present. Validate
   that its keys exactly match the routing allowed values. Represent descriptions
   with simple JSON strings initially. Keep label authority in `DECISION_VALUES`.
3. Pass those descriptions through the adapter into Jev’s `criteria`. Preserve
   the existing behavior for non-routing classifiers without criteria. Do not
   globally rewrite boolean, enum, entailment, or modifier classification.
4. Add a small set of generic grammatical contrasts to the routing criteria or
   instructions. If stored examples are used, explicitly serialize them once.
   Do not blindly serialize every existing classifier’s examples, or copy task
   cases into production prompts. Replace the illustrative state-machine wording
   from the earlier uncommitted edit with generic domains where appropriate.
5. For routing, build `state` from an explicit target and permitted source context.
   Put the question and route rules in the question definition. Remove duplicated
   request/workflow objects from this routing state, after proving needed source
   information survives. The surrounding source is evidence, not instructions
   that may override the classification contract.
   Preserve source-supported `scope_relations` for split clauses, including
   modifier attachments and Boolean combinations. Keep those distinct from prior
   model route labels. Represent and fingerprint any retained scope inputs in the
   shared inference contract so the dataset and local model see the same evidence.
6. Bump the routing request/prompt revision and ensure cached decisions become
   invalid when their interpretation contract changes. Inspect the existing
   revision mechanism before introducing another one. Version provider acceptance
   policy separately from semantic labels.

Jev documents per-choice descriptions, structured criteria, returned probabilities,
and confidence. This plan changes the request sent to the existing service; it
does not assume a customer fine-tuning API. See the official
[Choice documentation](https://docs.typesafe.ai/primitives/choice).

### A3. Make source context consistent

Implement bounded context once in the compiler input path and reuse it in data
collection, annotation, Jev evaluation, and local inference.

- Include the exact target, containing original sentence when useful, containing
  section heading when available, and immediate previous/next source sentences.
- Extend the current `_bounded_source_context` path and reuse compatible helpers
  in `instruction_context.py` and `format_classifier_input`; inspect their span
  assumptions before extending them. Derive neighbors from the original source,
  not from an arbitrary order of split clauses.
- Carry additional inputs through `command_catalog.py` and workflow bindings only
  if needed. Preserve callers that have only an isolated proposition.
- Preserve main’s tests showing neighbor context affects input fingerprints.
  Ensure all additional context that can change a decision is also fingerprinted.
  Prefer using/versioning the existing `context_text` contract if it can safely
  hold the canonical context; otherwise add a versioned field and update exact-key
  validation/serialization tests together. Do not silently put un-fingerprinted
  context into the request.
- Define and document deterministic bounds. Use at most one sentence on each
  side and one heading; cap each neighbor at 512 Unicode characters, retaining the
  previous sentence’s tail and next sentence’s head. Record truncation explicitly.
  Preserve the target verbatim. A target that exceeds the local model token limit
  must abstain or fall back, not be silently truncated.
- Do not supply solutions, test patches, prior predicted labels, gold annotations,
  or later generated scenarios as context.

Evaluate described criteria and any context refinements separately and together.
Use the same baseline revision and cases for the comparisons. This isolates an
improvement caused by better evidence from one caused by clearer choice wording.
The baseline already has bounded neighbors on main. Do not remove that existing
context merely to make the candidate comparison look better; record precisely
which additional context change each experimental variant makes. If no refinement
is needed, omit the duplicate context-only variant and document that decision.

### A4. Add meaningful tests and inspect downstream routing

Extend the existing test files. Required behavior checks:

1. Intercept the actual HTTP request in `_call_jev`; verify five described routes,
   the extra provider-abstention option, the exact target, and allowed context.
   Mocking `_call_jev` itself cannot detect malformed request bodies.
2. Verify prompt examples intended for Jev reach the request, without leaked gold
   annotations or duplicated whole-workflow context.
3. Exercise source-context plumbing from compiler preparation through adapter
   serialization, including split clauses, absent neighbors, and changed context
   invalidating a saved decision.
4. Preserve result mapping, malformed-answer fallback, explicit semantic `unclear`,
   and non-routing behavior. Test unsupported/extra criterion keys.
5. Bind a returned context route and assert that it creates no obligation or
   semantic-detail request while preserving the source as context. Bind a passive
   requirement and assert it retains the required product behavior.
6. Inspect the actual source-to-design output for the state-machine task and at
   least two different domains: background stays available, and modal, imperative,
   passive, and prohibition requirements remain included. Assert semantic behavior
   in deterministic contract artifacts; do not require exact generated prose.

Delete or replace the earlier test that merely repeats the stored example table
if these behavior tests cover it. Unit tests prove wiring; live labeled evaluation
proves model behavior. Report both separately.

### A5. Evaluate and decide what to ship

Run baseline, described-criteria-only, context-only, and combined variants. After
development selection, compare only baseline and the selected candidate on the
regression batch. Report raw per-class counts, confusion matrices, context
precision/recall, product obligations incorrectly routed to context/exclude,
abstentions, failures, latency, and provider identity. Count abstentions and
failures as non-correct in raw accuracy; also report accepted-only accuracy.

The Jev request change passes its initial gate when it improves context recall on
the frozen regression batch, does not increase lost product obligations, fixes the
original development failure, and passes all wiring/contract checks. Show counts,
not just percentages. A tie is inconclusive; expand fresh evaluation data before
claiming improvement. Correct descriptions alone are not an accuracy result.

Do not select a confidence threshold from these few examples. First deliver the
request fix and evidence. Calibrate a routing-only acceptance policy after the
larger reviewed development dataset exists in milestone B. Until then, document
the existing acceptance behavior and its limitations.

### A6. Calibrate Jev when enough reviewed data exists

On milestone B’s development/calibration data, choose per-label minimum top
probability and, if helpful, top-two margin. Record provider `confidence`
separately: it summarizes the distribution and is not interchangeable with the
top label probability. Do not assume either is calibrated for this dataset.

Implement the policy only for ordinary routing clients. A rejected prediction,
Jev `unresolved`, or malformed/missing distribution invokes the configured
planning fallback once. A sufficiently supported semantic `unclear` follows the
existing binder path to `unresolved/source_ambiguous`; it is not a transport
failure. Validate finite probabilities, expected labels, bounds, and a sum close
to one. Preserve the current rules for other classifier kinds and all fail-closed
clients; never bypass their policy to call planning.

Test that fallback happens at most once, failed fallback retains existing error
semantics, and an unresolved result never silently becomes an obligation, context,
or exclusion. Preserve main’s removal of the old benchmark include default.
Evaluate the actual Jev-plus-planning cascade too, including genuinely ambiguous
inputs that must stay unresolved through contract compilation.

## 5. Milestone B: build the routing dataset

### B1. Data format and provenance

Keep raw source, candidate annotations, finalized labels, splits, and predictions
in separate files under `science/classifications/routing/`. Use JSONL and a
manifest with hashes. Define this schema before collecting a large batch:

```json
{
  "schema_version": "routing-example-v1",
  "example_id": "routing:opaque-stable-id",
  "decision_kind": "routing",
  "input_revision": "routing-input-v1",
  "rubric_revision": "routing-rubric-v1",
  "source": {
    "document_id": "doc-0042",
    "family_id": "repository-or-independent-source-family",
    "origin": "real_instruction",
    "source_ref": "relative/source/reference",
    "source_sha256": "sha256-of-original-text",
    "target_span": {"start": 0, "end": 42},
    "derivation_group_id": "base-case-and-all-variants",
    "license": "recorded-source-license-or-unknown"
  },
  "inputs": {
    "proposition": "Exact target text",
    "local_context": {"source_sentence": "Exact containing sentence"},
    "scope_relations": null
  },
  "annotation": {
    "label": "context",
    "status": "proposed",
    "reviewer_kind": "assistant_model",
    "reviewer_id": "recorded-reviewer-or-model-revision",
    "rationale": "Existing limitation; no requested behavior in target.",
    "evidence_quotes": ["Exact relevant source words"],
    "ambiguity_reason": null
  },
  "slice_tags": ["present_state", "copular"],
  "split": "train"
}
```

The displayed span/hash values are schema placeholders. Real rows must validate
against retained source. For derived atomic clauses that are not a contiguous
substring, store the parent source span and explicit derivation instead of a
false target span. Keep every reviewer response in an append-only annotation
ledger keyed by example ID; the finalized row points to its resolution history.

Only `inputs` enters a model. Exclude IDs, tags, splits, rationales, labels,
reviewer metadata, and provenance from inference tensors and provider state.
Version and hash the exact inference serializer.
When present, `scope_relations` contains the compiler's source-supported split
relations. Serialize it deterministically alongside local context for both Jev
and local inference; keep absent relations null. Do not expose task-specific IDs
as semantic features: normalize relation references to positions within the
supplied source context, or omit unusable references consistently in both paths.

### B2. Collection and annotation workflow

1. Import existing task instructions and ledgers as raw candidates. Verify source
   hashes and spans. Keep old teacher labels in a separate silver-label record.
2. Translate old disposition labels only as candidate suggestions: product kinds
   usually suggest include, non_goal suggests include_prohibition, and context
   suggests context. These are not automatic gold mappings. Old nonactionable and
   unresolved labels especially need review because their semantics differ.
3. Sample broadly across repositories and domains. Include entire passages with
   multiple requirements, multiple context sentences, or no context sentences.
   Do not assume each passage contains exactly one sentence of each type.
   Start with local DeepSWE instruction files and retained ledgers. For fresh
   families, collect previously unused task instructions or original public issue
   and feature-request descriptions from different repositories. Preserve the
   original request text and source reference; exclude later solution discussions,
   implementation patches, and benchmark test oracles from classifier evidence.
4. For the initial pilot, collect roughly 1,000–1,500 candidates and review the
   first 200–300 diverse cases to stabilize the rubric. Revise inconsistent labels
   before scaling. This pilot can train an exploratory model but cannot satisfy
   all promotion gates by itself.
5. Target at least 2,500 reviewed base examples for the first promotion attempt:
   approximately 1,500 train, 400 development/calibration, and 600 fresh test.
   Split by family, so actual counts may vary. Keep training coverage of all five
   routes; aim for at least 200 training context examples and at least 100 each
   for include_prohibition and exclude. Add more data if grouping makes this
   impossible. Synthetic paraphrases do not count as independent base examples.
6. Keep at least 70% of the main corpus naturally occurring source instructions.
   Supplement training/development with reviewed contrast pairs and paraphrases.
   Give synthetic challenge results their own report. Training may oversample
   scarce classes; do not rebalance the natural test distribution silently.
7. Have reviewers label the target with exactly the context models will receive,
   without seeing teacher/Jev/local predictions. Reviewers record route, evidence,
   and short rationale. On the first batch, inspect agreement and confusion pairs
   and clarify the rubric. Do not resolve differences by majority model vote.
8. An assistant or teacher may draft labels and review suggestions, but retain
   its identity and mark them proposed/silver. Final human-reviewed labels need
   an actual human review record. Obtain independent labels for the fresh test
   set and adjudicate disagreements and low-certainty cases before scoring.
   If human review is pending, continue collection, tooling, and exploratory
   training; leave the corresponding gold/promotion gates explicitly incomplete.

Required slice coverage: current-state descriptions; limitations without “is”;
must/should/will requirements; imperatives; passive/declarative requirements;
product definitions; explicit prohibitions; negated background; process commands;
future-tense process context; quoted examples; heading-dependent meaning; mixed
sentences; pronoun/reference dependence; and genuine ambiguity. Include examples
where the identical target receives different labels under different headings.
Keep those variants in one derivation group.

Do not classify a lack of reviewer agreement as `unclear` automatically. Determine
whether the source is ambiguous or the rubric/reviewer made an error.

### B3. Leakage prevention and validation

- Assign repository/document families to splits before generating paraphrases.
  Keep every task from a repository, adjacent clauses, derived atomic clauses,
  contrast variants, and near duplicates in one split. If duplicate groups connect
  families, assign the connected group as a unit or remove it before splitting.
- Mark all previously inspected task families and all existing challenge cases as
  development for this new experiment. Preserve historical reports, but do not
  present their reused test examples as a new unseen evaluation.
- Reserve fresh families for the final test; never select prompt wording, model
  hyperparameters, checkpoint, temperature, or thresholds using their outcomes.
  If their errors guide another iteration, consume that test as development and
  collect a new frozen holdout.
- Require at least 300 product obligation examples, 150 context, 50 exclude, and
  50 unclear in the promotion holdout. Include at least 75 prohibitions among
  those product obligations. Increase the holdout size if family grouping or
  natural sampling requires it. Report enriched and natural slices separately.
- Validate unique IDs, source fidelity, legal labels, review status, split/group
  disjointness, input serialization, and no target loss after tokenization. Hash
  the split manifest and data. Fail loudly when a required class is absent.

Deliver `build_dataset.py`, `validate_dataset.py`, `prepare_review.py`,
`finalize_labels.py`, a split manifest, and a README with exact commands. Prefer
small reusable extensions to existing science helpers over copying an entire
training system. Do not modify historical datasets in place.

## 6. Milestone C: train a dedicated model

### C1. Baselines before another encoder run

Evaluate all baselines on the same finalized labels and inference inputs:

- Existing Jev request; repaired Jev; the configured planning model.
- A grammar heuristic that exposes its guesses and abstains on conflicts.
- A learned linear classifier using target word/character n-grams and explicit
  features for obligation words, imperative cues, copular/present-state wording,
  negation, and section context. Fit vocabulary/features on train only.

These are diagnostic baselines. Do not insert the heuristic ahead of Jev simply
because it fixes the state-machine example. Use the same five output routes for
the learned baseline; report a context-versus-rest diagnostic too. If adding
scikit-learn for this baseline, make it an optional training dependency.

### C2. Dedicated encoder training recipe

Use one five-way sequence classifier for `routing`. Start with the existing
`microsoft/MiniLM-L12-H384-uncased` backbone and pin its model/tokenizer revision.
The checkpoint supports Transformer fine-tuning; see the official
[model card](https://huggingface.co/microsoft/MiniLM-L12-H384-uncased) and
[sequence-classification guide](https://huggingface.co/docs/transformers/tasks/sequence_classification).
The installed repository environment is on Transformers 4.x; inspect its APIs
before copying examples from current online docs.

1. Put `train.py`, `predict.py`, and a versioned training configuration under the
   new routing directory. Share the exact inference serializer with Jev and the
   data validator. Do not train on task IDs, labels in prompts, or rationale text.
2. Use target text as the first tokenizer sequence and bounded local context plus
   normalized scope relations as the second. Preserve the complete target;
   truncate only neighboring context to a maximum
   total of 512 tokens. Reject/fallback when the target alone exceeds the model
   limit, or when target plus required scope evidence cannot fit. Apply the
   identical policy in training and prediction, and record context truncation.
3. Fix label order explicitly: include, include_prohibition, context, exclude,
   unclear. Persist both `label2id` and `id2label`; test saved-model round trips.
4. Start with AdamW, learning rate 2e-5, weight decay 0.01, effective batch size
   32, 10% warmup, maximum eight epochs, and validation each epoch. Stop after two
   epochs without validation improvement. Record device, runtime versions, and
   effective batch size. Use gradient accumulation if memory requires it.
5. Start with ordinary cross-entropy and deliberately sampled training coverage.
   Compare capped inverse-frequency weighting as a separate experiment, with
   maximum class-weight ratio 3. Do not combine aggressive weighting and
   oversampling without measuring it; the earlier all-context collapse makes
   this an explicit diagnostic concern, not an established cause.
6. Run seeds 17, 41, and 73. Use a small declared development search: learning
   rates 1e-5, 2e-5, and 5e-5, initially at seed 41; rerun the selected recipe at
   the other seeds. Select checkpoints on development macro F1 while also
   reporting context precision/recall and lost obligations. Never select on test.
7. Run a tiny training-only overfit check before expensive experiments: the model
   should learn a small batch of clearly labeled, balanced examples. If it cannot,
   inspect tokenization, label IDs, masks, gradients, and optimizer updates.
8. Print predicted class counts and confusion matrices every validation epoch.
   Stop and investigate collapse to one route, reversed labels, target truncation,
   or implausible calibration rather than launching more identical runs.

Treat hyperparameters above as starting settings, not promises of accuracy. If
MiniLM fails after verified inputs and reviewed data, inspect errors and label
consistency first. Only then compare a stronger encoder using the same splits and
report. Follow the broader local-classifier plan for that candidate; do not expand
this task into a multi-head model for every semantic dimension.

### C3. Calibration and candidate manifest

Use a fixed development partition for checkpoint/hyperparameter selection and a
disjoint calibration partition, grouped by family (start with 200 examples each;
expand if class counts are inadequate). Fit optional scalar temperature on the
calibration portion. Select per-label probability/margin thresholds there.

Low certainty produces provider `unresolved`, not semantic `unclear`. Measure
coverage so abstaining on everything cannot pass. Use silver training rows only
in a separately reported experiment; do not mix them into adjudicated evaluation.

Store model/tokenizer IDs and revisions, source/code hashes, input/rubric revisions,
split hashes, label map, hyperparameters, seeds, calibration values, thresholds,
metrics, latency, and `routing_ready` in a manifest. Default `routing_ready` to
false; only the explicit promotion evaluator can set it true after every gate.
Do not make readiness depend on a manually edited report field.

Keep model weights outside ordinary Git history. Before promotion, use the
repository’s artifact mechanism or an explicitly scoped LFS path, verify the
actual attributes, and store a checksum/download reference. Commit compact data,
scripts, manifests, and reports as appropriate. Preserve source licensing metadata
and confirm redistribution rights before publishing training text or weights.

## 7. Evaluation and promotion gates

These are proposed initial project acceptance targets. Record them in evaluator
configuration before opening the fresh holdout; do not relax them after a failure.

Always report raw model results, selectively accepted predictions, and the full
fallback cascade separately. Required metrics:

- Per-route precision, recall, F1, counts, and a full confusion matrix.
- Context precision and recall, including abstentions as missed context.
- Lost-obligation rate: true include/include_prohibition sent to context/exclude,
  divided by all true product obligations.
- Prohibition-to-positive inversions, semantic unclear counts, provider
  abstentions, transport failures, and fallback rate.
- Accepted coverage and accepted accuracy, overall and by route/domain.
- Calibration diagnostics and 95% uncertainty intervals. Report family-level
  variability; paraphrases are correlated and do not increase independent evidence.
- Warm CPU inference p50/p95, cold load time, hardware, memory, and total cascade
  latency/cost. Remote timing includes the network; local timing includes
  tokenization. Use repeated timings on a fixed batch after model selection.

### Dedicated-model deployment gates

1. All dataset validation, input fidelity, contract, and fallback tests pass.
2. On the fresh holdout, accepted context precision is at least 99%, context
   recall at least 90%, and total automatic coverage at least 80%.
3. Lost-obligation rate is at most 1%; report a one-sided 95% exact binomial upper
   bound and require it to be at most 1% on the enriched product subset too. With
   zero errors this needs at least 299 independent examples. Correlated examples
   weaken that interpretation: report grouped intervals and expand independent
   source coverage rather than counting paraphrases as independent trials.
4. No product-prohibition inversion occurs in the predefined critical boundary
   suite. Include passive requirements and negated background in that suite.
5. The complete local-to-Jev-to-planning cascade has context recall and macro F1
   no worse than the repaired Jev-to-planning baseline, and no higher observed
   lost-obligation rate on identical cases. Report uncertainty; if differences are
   unresolved, collect fresh evidence rather than claiming superiority.
6. Local warm CPU p95 is lower than measured Jev request p95 on the same input
   batch; the full cascade must not worsen measured end-to-end p95. Report cold
   startup separately. Do not infer speed from parameter count.
7. Seed results are reported; select the deployed candidate using development
   criteria, then run its final test once. Investigate large seed variance.

If data is too small for a gate, the result is “insufficient evidence,” not a pass.
Report a working prototype and continue data collection. Failed candidates remain
available for offline comparison and cannot route production.

## 8. Milestone D: integrate the qualified local router

Add a narrow routing provider wrapper consistent with the existing workflow
client protocol. Locate where Jev is registered before choosing the construction
site. Do not globally replace the planning client or change non-routing decisions.

```text
routing request
  -> qualified local model, when enabled
  -> qualified Jev routing policy on local abstention/unavailability
  -> configured planning fallback on Jev rejection/unavailability
  -> existing unresolved/review behavior if no provider can resolve
```

Non-routing calls and fail-closed clients follow their current path. Make the new
local path opt-in and use shadow evaluation first: record the local prediction
while the established
provider still controls the decision. Shadow disagreement is not a gold label.

Validate manifest compatibility, model checksum, label order, input revision, and
promotion status before loading. Missing/incompatible/corrupt artifacts or
oversized targets fall through to the established provider. Avoid loading weights
on every request. Set CPU as the deployment default and measure memory use.

Preserve provider provenance through an existing metadata channel, or introduce
a tested envelope without adding forbidden fields to classifier result schemas.
The current binder defaults provider kind to planning-llm; do not let local/remote
decisions be mislabeled in the new path. Inspect valid provider kinds before
extending them. Retain request-level telemetry without logging secrets or full
source text by default.

Test enabled/disabled behavior, accepted predictions, local abstention, Jev
abstention, invalid artifacts, fallback failure, source-context hashing, and
unaffected non-routing decisions. Capture downstream contract/prompt evidence for
the state-machine development case and other domains after integration. Rollback
disables the local wrapper and restores the established Jev/planning route.

## 9. Commands and PR sequence

The following routing CLIs are to be implemented; they do not exist merely because
this plan names them. Make `--help` document required inputs, output paths, seeds,
revisions, and whether the command calls a remote provider. Use explicit input and
output paths, resumable example IDs, and run manifests. Never overwrite an earlier
evaluation in place.

```bash
export ROUTING_DIR=science/classifications/routing

# Milestone A: serialize requests without spending provider tokens.
rtk proxy "$VIRTUAL_ENV/bin/python" "$ROUTING_DIR/evaluate.py" \
  --dataset "$ROUTING_DIR/data/jev-development.jsonl" \
  --provider jev --variant described-criteria --dry-run \
  --output-dir "$ROUTING_DIR/reports/jev-development-criteria-requests"

# Capture the baseline before editing the adapter. Store its code/request hashes.
rtk proxy "$VIRTUAL_ENV/bin/python" "$ROUTING_DIR/evaluate.py" \
  --dataset "$ROUTING_DIR/data/jev-development.jsonl" \
  --provider jev --variant baseline --allow-live \
  --output-dir "$ROUTING_DIR/reports/jev-development-baseline"

# After implementing and freezing the combined candidate:
rtk proxy "$VIRTUAL_ENV/bin/python" "$ROUTING_DIR/evaluate.py" \
  --dataset "$ROUTING_DIR/data/jev-regression.jsonl" \
  --provider jev --variant combined --allow-live \
  --output-dir "$ROUTING_DIR/reports/jev-regression-combined"

# Milestone B: reject invalid reviews, spans, or leaking splits.
rtk proxy "$VIRTUAL_ENV/bin/python" "$ROUTING_DIR/validate_dataset.py" \
  --dataset "$ROUTING_DIR/data/routing-v1.jsonl" \
  --split-manifest "$ROUTING_DIR/data/splits-v1.json" \
  --require-reviewed

# Milestone C: config contains pinned backbone and tokenizer, explicit data paths,
# split hashes, input revision, and the recipe from section C2.
rtk proxy "$VIRTUAL_ENV/bin/python" "$ROUTING_DIR/train.py" \
  --config "$ROUTING_DIR/configs/minilm-routing-v1.json" --seed 41 \
  --output-dir /private/tmp/powdrr-routing-artifacts/minilm-routing-v1-seed41

# Freeze the selected model and calibration before running this final evaluation.
rtk proxy "$VIRTUAL_ENV/bin/python" "$ROUTING_DIR/evaluate.py" \
  --dataset "$ROUTING_DIR/data/routing-v1.jsonl" --split test \
  --provider local \
  --model-manifest /private/tmp/powdrr-routing-artifacts/minilm-routing-v1-seed41/manifest.json \
  --output-dir "$ROUTING_DIR/reports/minilm-routing-v1-final"
```

Implement the shown CLI options as the minimum interface. Add `--model` for an
explicit Jev revision and require it to match the frozen run configuration for
final comparisons. Baseline/criteria/context/combined variants must invoke shared
production builders or a frozen baseline builder, not independently handwritten
prompts. The evaluator also needs a planning-provider mode and a report-only mode
for saved predictions. Use a new output directory for every distinct run.

Write exact runnable commands with actual filenames and revisions in the routing
README as each milestone lands. Do not leave the handoff recipient to infer
default paths or whether a run used a teacher label versus a reviewed label.
Maintain a small `STATUS.md` with the current milestone, completed PRs, frozen
dataset/config hashes, latest reports, failed gates, and the next command. Another
agent should be able to resume without reconstructing the conversation.

Suggested PRs, in order:

| PR | Deliverable | Evidence required |
| --- | --- | --- |
| 1 | Jev criteria, explicit source context, request serialization, focused evaluation harness and initial cases. | Before/after live predictions, HTTP payload tests, contract regressions, full repository checks. |
| 2 | Routing annotation guide, collection/review/validation tools, first reviewed corpus, split manifests; calibrated Jev routing policy when supported by the data. | Label provenance, review coverage, leakage checks, Jev policy/cascade report. If calibration is still underpowered, keep it explicitly disabled. |
| 3 | Linear baseline, dedicated encoder, training/prediction tools, reproducible reports and candidate manifest. | Frozen splits, development selection, calibration and fresh test results, model readiness gate. A failed model can land as research with readiness false. |
| 4 | Qualified local provider, shadow mode, fallback/provenance plumbing, activation configuration. | All promotion gates, actual cascade evaluation and downstream evidence. Do not open as ready to deploy if the model failed. |

Before pushing each implementation PR, run focused tests first, then the full
repository checks once changes are stable:

```bash
rtk proxy "$VIRTUAL_ENV/bin/python" -m pytest tests/test_jev_classifier.py tests/test_semantic_contract_compiler.py tests/test_semantic_decision.py tests/test_root_disposition_context_retry.py
rtk proxy "$VIRTUAL_ENV/bin/python" -m pytest
rtk proxy "$VIRTUAL_ENV/bin/python" -m ruff format --check .
rtk proxy "$VIRTUAL_ENV/bin/python" -m ruff check .
rtk proxy "$VIRTUAL_ENV/bin/python" -m mypy src tests
rtk git diff --check
```

Check any additional repository validation required by the changed files. Explain
pre-existing failures with evidence; do not fix unrelated code to make a routing
PR green. Stage only the intended change set, inspect the staged diff, push a
feature branch, and open a PR. The user reviews and merges it.

## 10. Completion checklist for the implementing agent

- [ ] Actual Jev HTTP payload contains useful routing descriptions and the intended examples/context.
- [ ] Generic grammar cues improve held-out behavior without losing real requirements.
- [ ] Unit-test success is reported separately from live model accuracy.
- [ ] Dataset rows distinguish old disposition labels, routing labels, silver proposals, and final reviews.
- [ ] Fresh holdout families and all derivation groups are protected from tuning.
- [ ] Local model input exactly matches runtime input, including truncation and context handling.
- [ ] Baselines, collapse checks, class counts, calibration, uncertainty, and fallback coverage are reported.
- [ ] No failed experiment is marked routing-ready or deployed.
- [ ] Context remains available downstream and creates no obligation; requirements and prohibitions survive.
- [ ] Reviewable PRs contain only scoped changes and compact artifacts; all required checks are documented.

When handing back progress, name the milestone completed, link its PR/report,
state the measured change in context recall and lost obligations, and identify
any remaining data or qualification gate. Do not summarize a prompt edit as a
trained model, or an authored example table as demonstrated generalization.
