# Local Semantic Classifier Implementation Plan

## Purpose

This plan defines how to build local specialized models for every classifier
and extractor in
`docs/design/source-anchored-semantic-contract-compilation.md`. The goal is not
merely to replace an API call with a smaller model. The local path must:

- equal or exceed the current planning LLM's accuracy on a frozen, adjudicated
  evaluation suite;
- be materially faster end to end;
- run inference on ordinary CPU hardware by default;
- preserve the provider-neutral semantic-decision contract;
- abstain and fall back when a local prediction is not sufficiently reliable;
- feed deterministic design assembly whose terminal output is one immutable
  prompt for one mini-SWE-agent implementation invocation;
- remain reproducible from pinned data, code, model, tokenizer, and runtime
  revisions; and
- support replacement one decision kind at a time without changing Procedrr.

The recommended production target is a small portfolio, not one model:

```text
deterministic lexical and repository rules
  -> clause classifier bundle
  -> exact-span extractor bundle
  -> semantic pair classifier bundle
  -> planning LLM or human only for abstentions
```

All three learned bundles are CPU-capable at inference time. A GPU is strongly
recommended for repeatable full fine-tuning, but no GPU is required in the
Powdrr runtime. CPU-only prototype training remains possible for the earliest
SetFit and linear baselines.

## Relationship to the semantic-contract design

The source-anchored design remains authoritative for:

- classifier labels and meanings;
- exact extraction requirements;
- the common decision envelope;
- unresolved reason codes;
- deterministic lookup and assembly;
- field-level provenance;
- source-faithfulness gates; and
- classifier promotion requirements.

This plan adds the missing machine-learning implementation details:

- model partitioning;
- backbone selection;
- dataset schemas and collection;
- adjudication and leakage prevention;
- task-specific training examples and losses;
- probability calibration and abstention;
- comparison against the existing LLM;
- CPU optimization and packaging;
- hardware requirements;
- runtime integration;
- monitoring and retraining; and
- an incremental PR sequence.

It does not change classifier semantics. A trained model that requires a label
change has discovered a design issue; it does not have authority to redefine
the label.

## Downstream compilation target

Local classifiers do not produce worker tasks. They resolve fields in the
canonical design. After all fields, repository bindings, populations,
predicates, and verification cases are complete, deterministic code compiles
the entire accepted design into one worker-facing
`minisweagent-implementation-prompt-v1` and one private
`obligation-validation-manifest-v1`.

Exactly one prompt is sent to mini-SWE-agent exactly once. The classifier
provider cascade may use many small local decisions during design, but those
decisions must not leak into multiple implementation prompts. mini-SWE-agent's
internal inspect/edit/test turns are part of one invocation and one prompt.

The private manifest preserves every obligation, verification case, oracle,
baseline expectation, and required evidence kind for post-coding validation.
Classifier replacement must leave both projections semantically equivalent:
the prompt and manifest must contain the same contract and case sets even
though only the prompt is sent to the worker.

Post-worker validation is deterministically orchestrated from the private
manifest. Collection, execution, baseline differential, scope, and supported
oracle checks are deterministic; irreducibly semantic checks use bounded
read-only judges one obligation at a time. Validation may accept or reject the
result, but it does not create a repair, continuation, per-obligation, or
fallback worker prompt. Failed evidence can become input to a later design
revision and new run, never another prompt in the same run.

## Decision summary

### Production model portfolio

| Bundle | Responsibilities | CPU-first backbone | Accuracy fallback |
| --- | --- | --- | --- |
| Deterministic resolver | Explicit lexical labels, exact aliases, source substring validation, repository identity matches | Ordinary Python/Rust code | None; no rule match falls through |
| Clause classifier | Atomicity plus C01-C07 and source-only portion of C08 | MiniLM-L12-H384 with task-specific heads | ModernBERT-base with the same heads |
| Span extractor | E01-E05 exact source spans | MiniLM-L12-H384 token encoder with five overlapping BIO heads | ModernBERT-base token encoder |
| Semantic pair classifier | Definition-backed C08, C09 candidate relation, C10 entailment, and C11 proposition coverage | MiniLM-L12-H384 cross-encoder with task-specific heads | ModernBERT-base cross-encoder |

The CPU-first MiniLM checkpoint is a 33M-parameter, 12-layer, 384-hidden model
published as a BERT-compatible fine-tuning base. Its model card reports strong
NLU results at substantially lower cost than BERT-base and uses an MIT license:
[MiniLM-L12-H384 model card](https://huggingface.co/microsoft/MiniLM-L12-H384-uncased).

ModernBERT-base is a 149M-parameter encoder with code in its pretraining mix and
strong classification and retrieval results. It is not the default because it
is larger and its best published efficiency uses GPU-oriented kernels. It is
the first fallback when the small model misses accuracy, before considering a
generative model: [ModernBERT introduction](https://huggingface.co/blog/modernbert).

### Why three learned bundles

The decision shapes are materially different:

- C01-C07 mostly classify one short proposition or extracted phrase.
- E01-E05 locate one or more exact token spans and must support overlapping
  semantic roles.
- C08-C11 compare two semantic objects and need cross-attention between them.

A single model can technically expose every output, but it complicates losses,
calibration, optimization, and failure analysis. Three shared encoders preserve
most efficiency while keeping each learned problem coherent.

### Why not one model per classifier

One model per classifier would simplify isolated training but would produce too
many model files, repeated tokenization, excessive cold-start time, and sparse
datasets. Closely related tasks should share an encoder and have independent
heads. If multi-task interference causes a decision kind to miss its promotion
gate by more than one percentage point, fork only that task into a separate
bundle and record the reason in its manifest.

### Jev's role

Jev is an external System One service designed for typed decisions and
calibrated probabilities. TypeSafe reports 70-500 ms service latency and
positions it for classification, scoring, extraction, and branching:
[TypeSafe's Jev introduction](https://typesafe.ai/blog/introducing-system-one-models-and-jev).
It is a useful benchmark and potentially a provider in the fallback cascade,
but it is not a locally trainable Powdrr model and must not be included in the
local artifact bundle. Local models are compared separately against:

1. the current planning LLM;
2. Jev when access and the decision shape permit; and
3. deterministic rules where an exact rule exists.

## Classifier coverage matrix

The following table is exhaustive for the source-anchored design. `AC01` is
included because atomicity precedes the catalog and controls whether the later
classifiers receive a valid proposition.

| ID | Decision | First resolver | Learned bundle | Head and output |
| --- | --- | --- | --- | --- |
| AC01 | Clause contains multiple independently verifiable requirements | Punctuation/conjunction hints may return only `no_decision` | Clause classifier | Binary `atomic`, `multiple`, plus abstention |
| C01 | Disposition | Exact process phrases only | Clause classifier | 7 semantic labels plus calibrated abstention |
| C02 | Polarity | Explicit modal-negation patterns | Clause classifier | 4 semantic labels; abstention outside head |
| C03 | Quantifier | Exact `all`, `every`, `one`, `some` patterns | Clause classifier | 4 semantic labels |
| C04 | Requirement strength | Exact `must`, `should`, `may` patterns | Clause classifier | 5 semantic labels |
| E01 | Subject span | Exact compiler-supplied symbol only when unique | Span extractor | Subject BIO head |
| E02 | Behavior span | No general semantic rule | Span extractor | Behavior BIO head |
| C05 | Behavior family | Exact accepted ontology term | Clause classifier | Family head over registered values |
| C06a | Precondition presence | Explicit registered condition markers may resolve | Clause classifier | Binary presence head |
| C06b | Exception presence | Explicit `except`, `unless`, `other than` patterns | Clause classifier | Binary presence head |
| C06c | Explicit-result presence | Registered result syntax may resolve | Clause classifier | Binary presence head |
| E03 | Precondition span | Exact clause parser when unambiguous | Span extractor | Precondition BIO head |
| E04 | Exception span | Exact clause parser when unambiguous | Span extractor | Exception BIO head |
| E05 | Explicit-result span | Exact clause parser when unambiguous | Span extractor | Result BIO head |
| C07 | Temporal scope | Explicit temporal phrase table | Clause classifier | 5 semantic labels |
| C08 | Source predicate | Exact source result or exact accepted term definition | Clause classifier for source-only state; pair classifier for term-definition implication | 4-way head |
| C09 | Candidate relation | Exact canonical/accepted alias binding | Semantic pair classifier | 3-way match head |
| C10 | Field entailment | Exact compiler proof for lexical fields | Semantic pair classifier | 4-way NLI head |
| C11 | Proposition coverage | Exact normalized equality only | Semantic pair classifier | 5-way coverage head |

`unresolved` is not generally learned as an ordinary semantic class. Each model
predicts semantic labels and calibrated probabilities. Runtime abstention
produces `unresolved` with `classifier_abstained` when the accepted prediction
set is empty or non-singleton. Examples that are inherently ambiguous remain
in evaluation data and train an auxiliary `answerable` head where needed.

## Decision-specific corpus requirements

Each decision kind needs purpose-built boundary data. Shared encoders do not
justify pooling labels without preserving these task-specific requirements.

| Decision | Required positive coverage | Required confusions and hard cases | Primary promotion metric |
| --- | --- | --- | --- |
| AC01 atomicity | Single behavior with modifiers; multiple independently testable behaviors | Lists hidden in prose, one behavior with several inputs, one validation with several failure causes | Recall for `multiple` without over-splitting atomic clauses |
| C01 disposition | Every semantic label across product and process language | `non_goal` vs `nonactionable`, `guidance` vs `invariant`, missing capability context vs descriptive text | Per-label precision/recall; zero critical intent erasure |
| C02 polarity | Required, prohibited, permitted, descriptive | Nested negation, absence descriptions, “without” context, double negatives | Precision for `prohibited` and required/prohibited inversion rate |
| C03 quantifier | One, some, every, unspecified | Universal noun forms, implicit singulars, conditions that must not become quantifiers | Precision for `every`; false narrowing/expansion |
| C04 strength | Must, should, may, descriptive, unspecified | Polite requests, future tense, “can” as capability vs permission | Per-label accuracy and must/should/may confusion |
| E01 subject | Actors, entities, interfaces, populations, invocation classes | Determiners, coordinated nouns, nested possessives, repeated text | Exact minimal character-span match |
| E02 behavior | Verbs plus required objects/results | Dropped format/error modifiers, negated verbs, coordinated operations | Exact minimal span and semantic-completeness review |
| C05 family | Every registered family with ordinary synonyms | Same verb in different domains, serialization vs round trip, persist vs serialize | Macro F1 and ontology-candidate recall |
| C06a precondition | If/when/while clauses and adjectival restrictions | Temporal sequence vs condition, universal subject modifiers | Presence precision and recall |
| C06b exception | Unless/except/other-than cases | Negative conditions that are not exceptions | Presence precision, with priority on recall |
| C06c explicit result | Returned values, raised errors, state changes | Purpose statements, implementation descriptions, implied outcomes | Presence precision and recall |
| E03 precondition | Complete condition including comparator/value | Dropped negation, detached subject, multiple conditions | Exact span and predicate round-trip |
| E04 exception | Complete exclusion span | Unless-clause polarity, enumerated exceptions | Exact span and polarity preservation |
| E05 result | Complete observable outcome | Mechanism mistaken for result, partial value/format spans | Exact span and result completeness |
| C07 temporal scope | Current, future, both, event-bound, unspecified | “Always” as quantifier vs time, lifecycle events, migration-only scope | Macro F1 and false future-scope expansion |
| C08 source predicate | Explicit, definition-implied, not stated | Reasonable but unstated quality, ontology candidate without authority | Precision for `explicit`/`implied`; unstated recall |
| C09 candidate relation | Exact semantic matches across symbol kinds | Siblings, homonyms, lexical match with wrong kind, stale aliases | Match precision and accepted-binding accuracy |
| C10 entailment | Entailed, contradicted, not stated | Quantifier/polarity changes, condition loss, plausible additions | Per-label F1; contradicted/not-stated precision |
| C11 coverage | Full, partial, absent, invented | Overlap, omission, detached constraints, added implementation detail | Full-coverage precision and invention recall |

For every row, the annotation guide must state whether a false positive causes
intent expansion, intent narrowing, or only extra fallback work. Sampling and
thresholds should emphasize the more damaging direction rather than treating
all errors as interchangeable.

## Model A: clause classifier bundle

### Tasks

The bundle contains independent heads for:

- AC01 atomicity;
- C01 disposition;
- C02 polarity;
- C03 quantifier;
- C04 requirement strength;
- C05 behavior family;
- C06a precondition presence;
- C06b exception presence;
- C06c explicit-result presence;
- C07 temporal scope;
- C08 source-predicate state when no external definition is supplied; and
- answerability for each semantic head.

### Input representation

Every example uses the same typed input:

```text
[CLS] [TASK=C03] proposition text [SEP]
```

Optional pronoun-resolution context is represented separately:

```text
[CLS] [TASK=C01] proposition text [SEP] bounded parent context [SEP]
```

The task marker is a registered tokenizer special token. Do not express the
task as a long natural-language prompt; label definitions belong to the model
contract and training data. Maximum sequence length is 128 tokens for ordinary
clauses and 256 only for parent-context cases.

### Architecture

Use one encoder and one linear classification head per decision kind. The
answerability head can share the pooled encoder state but has a separate binary
loss. At inference time, cache the encoder state by:

```text
(model_revision, tokenizer_revision, proposition_fingerprint,
 parent_context_fingerprint, max_length)
```

All requested heads for the same proposition then evaluate through matrix
multiplication without another transformer pass. Procedrr still consumes one
decision at a time; cached multi-head computation is an implementation
optimization, not a multi-decision model response.

### Loss

For each training batch:

```text
loss = semantic_cross_entropy + 0.25 * answerability_binary_cross_entropy
```

Use class-balanced sampling before introducing class weights. If a critical
minority class remains underlearned, use bounded inverse-square-root class
weights capped at 3.0. Do not use unbounded inverse-frequency weights.

### SetFit prototype

Before full fine-tuning, train one SetFit model per small-label task using the
same splits. SetFit is specifically designed for low-latency classification
with little labeled data and supports efficient contrastive fine-tuning:
[SetFit quickstart](https://huggingface.co/docs/setfit/en/quickstart).

Use this prototype to answer two questions:

1. Is the current dataset separable with a small embedding model?
2. How much does a shared transformer improve over a cheap embedding plus
   logistic head?

If SetFit meets every task's final promotion gate, it may remain the production
implementation for that task. Do not replace a passing simpler model merely
because a transformer is newer.

## Model B: exact-span extractor bundle

### Tasks and overlap

E01-E05 can overlap. For example, a result phrase may be contained within a
behavior phrase. A single mutually exclusive BIO tag sequence would make this
impossible. Use five independent BIO heads over one token encoding:

```text
subject:      O B I I O ...
behavior:     O O O B I ...
precondition: O O O O O ...
exception:    O O O O O ...
result:       O O O O I ...
```

Each head predicts `B`, `I`, or `O`. The runtime decodes at most the configured
number of spans for that role; E01 and E02 initially require exactly one or
abstain. E03-E05 may return several compiler-indexed candidates, which
Procedrr evaluates one at a time.

Token classification is a standard encoder fine-tuning task, and Hugging Face
documents training and inference with exact character offsets:
[Transformers token-classification guide](https://huggingface.co/docs/transformers/main/tasks/token_classification).

### Labels and offsets

Gold records store character spans against immutable ledger text. During
tokenization:

1. request offset mappings from a fast tokenizer;
2. mark the first token intersecting a gold span `B`;
3. mark subsequent intersecting tokens `I`;
4. mask special tokens and context-only tokens from loss;
5. reject training examples whose offsets cannot round-trip to the exact gold
   substring; and
6. preserve whitespace and punctuation in the final Workrr span, not in the
   tokenizer's reconstructed text.

### Architecture and loss

Use the same MiniLM backbone with five independent token-classification heads.
Sum role losses only for roles annotated in the example:

```text
loss = sum(role_cross_entropy * role_present_mask)
```

Use a constrained BIO decoder:

- `I` cannot begin a span;
- `I` following `O` is treated as an invalid path, not silently changed to
  `B`;
- special and context tokens cannot be selected;
- decoded text must pass exact-source span validation; and
- overlapping spans are legal across heads but not within one role unless the
  extractor contract permits multiple spans.

A conditional random field is optional. Add it only if the plain constrained
decoder misses the exact-match gate; it complicates ONNX export and CPU
optimization.

### Alternative span-ranker experiment

Train a pairwise span ranker only as a bake-off:

```text
[CLS] [ROLE=SUBJECT] proposition [SEP] candidate span [SEP]
```

Candidate spans come from a deterministic parser and bounded contiguous token
windows. This has perfect extractive validity but can suffer candidate recall
loss. Promote it over token classification only if:

- candidate recall exceeds 99.9% on held-out gold spans;
- exact-match accuracy meets the LLM baseline;
- end-to-end latency, including candidate generation and scoring, is lower;
  and
- candidate overflow is below 0.1%.

## Model C: semantic pair classifier bundle

### Tasks

The bundle handles:

- C08: whether one accepted definition supplies a source predicate;
- C09: whether one repository or ontology candidate denotes one source
  concept;
- C10: whether one normalized field assertion is entailed by, contradicted by,
  or absent from one authority; and
- C11: whether one child proposition covers one source proposition.

### Input representation

Use one cross-encoder input so every token in the first semantic object can
attend to every token in the second:

```text
[CLS] [TASK=C10] authoritative proposition [SEP] field assertion [SEP]
```

For C09, serialize candidate evidence in a stable bounded order:

```text
[CLS] [TASK=C09] proposition + marked subject span [SEP]
candidate canonical name; kind; accepted description; evidence reasons [SEP]
```

Do not include candidate score, rank, or neighboring candidates. Those values
can bias the semantic decision and are not semantic evidence.

Cross-encoders are designed for pair classification, including multi-class
natural-language inference, and can be initialized from MiniLM or ModernBERT:
[Sentence Transformers cross-encoder training](https://sbert.net/docs/cross_encoder/training_overview.html).

### Heads

Use separate heads and label maps:

```text
C08: explicit | implied_by_registered_term | not_stated | unresolved_evidence
C09: matches | does_not_match | insufficient_evidence
C10: entailed | contradicted | not_stated | unresolved_evidence
C11: fully_represented | partly_represented | not_represented | invented |
     unresolved_evidence
```

`unresolved_evidence` is a semantic judgment that the supplied pair lacks
evidence. It is distinct from runtime abstention caused by uncertain model
probabilities.

### Hard negatives

Random negatives make C09-C11 look artificially easy. At least 70% of negative
training pairs must be hard negatives drawn from:

- candidates with the same normalized nouns but the wrong repository kind;
- sibling symbols in the same component;
- aliases belonging to another Structrr entity;
- partially overlapping atomic propositions;
- assertions that preserve nouns but change polarity;
- assertions that narrow `every` to `some` or one member;
- assertions that detach a condition or exception;
- plausible predicates not stated by the source; and
- child propositions that add one implementation detail.

Sentence Transformers explicitly notes that cross-encoders benefit from strong
hard negatives. Use the pair model's own high-scoring errors for active hard
negative mining after the first training round.

## Dataset contracts

### Canonical example

All models train from `semantic-classifier-example-v1`:

```yaml
schema_version: semantic-classifier-example-v1
example_id: example:c03:000001
decision_kind: C03
decision_contract_revision: quantifier-v1
inputs:
  proposition: All data should pickle.
  parent_context: null
  left_text: null
  right_text: null
  candidate_evidence: null
labels:
  class: every
  spans: []
  answerable: true
source:
  origin: production | authored_boundary | synthetic_reviewed | repository_case
  source_ref: conversation:.../message:...
  source_family_id: family:pickle-requirement
  repository_id: optional
adjudication:
  status: gold
  adjudicators: [reviewer-a, reviewer-b]
  resolution: agreement | adjudicated
  notes_ref: optional-artifact
licenses: [repository-license-or-dataset-license]
```

Training code refuses:

- examples without `gold` adjudication;
- unknown decision-contract revisions;
- labels absent from the registered label map;
- invalid or non-round-tripping spans;
- duplicate normalized inputs with conflicting labels unless an adjudication
  record resolves them; and
- examples whose source or license cannot be traced.

### Pair example extension

Pair tasks populate `left_text`, `right_text`, and structured candidate
evidence. They also store a `pair_family_id` so positive and hard-negative
variants remain in one dataset split.

### Annotation guide

Create one guide per decision kind containing:

- exact label definitions copied from the semantic design;
- inclusion and exclusion rules;
- at least 20 ordinary examples;
- at least 20 boundary examples;
- at least 10 counterexamples for each high-risk confusion pair;
- explicit unresolved cases;
- span-minimality rules for extractors; and
- an adjudication decision tree.

The guide revision is part of the dataset revision. Annotation cannot begin
for a decision kind until its guide has passed review.

### Gold-label process

1. One annotator labels the example without seeing the current LLM result.
2. A second annotator independently labels it.
3. Agreement becomes provisional gold.
4. Disagreement goes to an adjudicator who sees both rationales and the
   authoritative classifier contract.
5. The adjudicated result is final for that dataset revision.
6. Model or LLM disagreement with gold creates an error-analysis item, not an
   automatic relabel.

For source spans, compare exact offsets before semantic adjudication. For C09,
annotators receive the same bounded candidate evidence as the model, not full
repository access.

### Sources of examples

Use, in descending authority:

1. accepted Powdrr instruction clauses and reviewed intermediate decisions;
2. manually authored boundary and minimal-pair cases;
3. repository-grounded candidate relations sampled from real Structrr
   inventories;
4. production abstentions and disagreements after human resolution; and
5. LLM-generated or mutation-generated examples only after human review.

Existing unreviewed planning outputs are weak labels. They may prioritize
annotation but never enter gold training or test data directly.

### Minimal-pair generation

Programmatically derive review candidates by changing exactly one semantic
dimension:

- `all` <-> `some`;
- `must` <-> `may`;
- positive <-> prohibited;
- condition present <-> absent;
- exception retained <-> removed;
- current <-> future;
- exact subject <-> sibling subject;
- entailed predicate <-> plausible but unstated predicate; and
- full proposition <-> proposition with one added implementation detail.

Generated pairs remain in the same split and require human review. Their value
is boundary density, not cheap volume.

## Dataset splitting and leakage control

Use four immutable partitions:

| Partition | Purpose | May affect training? |
| --- | --- | --- |
| `train` | Weight fitting and hard-negative mining | Yes |
| `development` | Hyperparameters, architecture, early stopping | Yes |
| `calibration` | Temperature and abstention thresholds | No weight updates |
| `held_out` | Final comparison and promotion | Never |

Split by `source_family_id`, not row. Also group by:

- originating user message;
- feature/work-item lineage;
- repository and symbol family for C09;
- mutation/minimal-pair family;
- template or synthetic seed; and
- paraphrase cluster.

No group can cross partitions. Repository generalization reports must include a
leave-one-repository-out evaluation when enough repositories are available.

Suggested initial proportions are 70/10/10/10, but held-out support takes
precedence. Before promotion, collect at least:

- 300 held-out examples for each high-risk label (`prohibited`, `every`,
  exception present, `contradicted`, `invented`);
- 200 held-out examples for every other label;
- 1,000 exact spans across all extractor roles, with at least 150 per role; and
- 2,000 pair examples per pair-classifier task, emphasizing hard negatives.

These are evaluation minima, not claims about training sample sufficiency.
Generate learning curves at 25%, 50%, 75%, and 100% of training data before
deciding whether more labels or a larger model are needed.

## LLM baseline protocol

The goal is accuracy relative to a fixed baseline, so freeze it before model
selection:

```yaml
schema_version: semantic-classifier-baseline-v1
provider: current-planning-provider
model: exact-provider-model-id
prompt_contract_revision: exact-revision
temperature: exact-value
attempt_policy: exact-policy
dataset_revision: sha256:...
run_ids: [...]
```

Run the current LLM classifier on the held-out set at least three times if the
provider is nondeterministic. Report:

- first-run accuracy;
- majority-vote accuracy for analysis only;
- inter-run disagreement;
- invalid response rate;
- abstention rate;
- p50/p95/p99 latency;
- input/output tokens; and
- provider cost.

The production comparison is against the actual first-run policy, not a
majority vote Powdrr does not currently use.

Jev, if evaluated, receives the same semantic question and allowed values. It
is reported as a separate external decision-model baseline.

## Training procedure

### Stage 0: deterministic baseline

Implement and evaluate exact rules first. Record coverage and precision. A
rule is promoted only at 100% precision on held-out examples that it resolves;
otherwise narrow it until it reaches 100% or remove it.

### Stage 1: cheap model bake-off

For each task family, train:

- majority and stratified random baselines;
- TF-IDF character/word n-grams with logistic regression;
- SetFit with a small sentence-transformer backbone;
- MiniLM full fine-tuning; and
- ModernBERT-base only if MiniLM misses the accuracy gate.

The simple baselines are mandatory. If TF-IDF wins on explicit modal labels,
shipping a transformer would add operational risk without value.

### Stage 2: full fine-tuning

Initial search space:

```yaml
seeds: [17, 29, 43]
learning_rate: [1.0e-5, 2.0e-5, 3.0e-5]
weight_decay: [0.0, 0.01]
epochs: [3, 5, 8]
effective_batch_size: [32, 64]
warmup_ratio: [0.0, 0.06]
max_length:
  clause: 128
  span: 256
  pair: 256
```

Use AdamW, gradient clipping at 1.0, early stopping on the task-specific
risk-weighted development metric, and save predictions for every candidate
checkpoint. Select architecture and hyperparameters on development data only.
Select one seed policy before the held-out run: either the median development
seed or an explicitly trained ensemble. Do not pick the best held-out seed.

### Multi-task sampling

Use temperature-scaled task sampling so large tasks do not erase small ones:

```text
p(task) proportional to example_count(task) ** 0.5
```

Every optimizer batch contains one task and updates the shared encoder plus
that task's head. Compare against round-robin sampling. If a task loses more
than one percentage point relative to its single-task model, inspect gradient
conflict and either rebalance or fork it.

### Distillation

Do not treat planning-LLM answers as gold. Optional distillation may use teacher
probabilities only on examples that already have gold labels, and the gold loss
must remain dominant:

```text
loss = 0.8 * gold_loss + 0.2 * teacher_distribution_loss
```

Promote distillation only when it improves held-out accuracy without reducing
critical-label precision or calibration.

## Calibration and abstention

Raw neural probabilities are not trustworthy enough for control flow. Modern
neural networks can be miscalibrated; temperature scaling is a simple validated
post-training method: [On Calibration of Modern Neural Networks](https://proceedings.mlr.press/v70/guo17a/guo17a.pdf).

### Calibration sequence

For every head:

1. freeze weights;
2. fit one temperature on the calibration partition;
3. compute expected calibration error and reliability diagrams;
4. choose class-specific acceptance thresholds;
5. choose a minimum top-two probability margin; and
6. seal temperatures and thresholds into the model manifest.

### Acceptance rule

A prediction is accepted only when all are true:

```text
answerable_probability >= answerable_threshold
top_class_probability >= threshold[top_class]
top_class_probability - second_probability >= margin[decision_kind]
input passes length and schema constraints
model and decision contract revisions match
```

Otherwise return:

```json
{
  "status": "unresolved",
  "reason_code": "classifier_abstained"
}
```

For critical classes, tune thresholds to precision first, then measure
coverage. Do not lower thresholds merely to reduce fallback calls.

### Optional conformal prediction

Evaluate class-conditional conformal prediction after temperature scaling. A
singleton prediction set is accepted; an empty or multi-label set abstains.
Adopt it only if class-wise coverage is more stable under repository shift than
fixed thresholds and local coverage remains useful. The fixed-threshold path
must remain the simpler reference implementation.

## Accuracy and promotion gates

### Metrics by task type

Clause classifiers report:

- accuracy;
- macro and per-label precision, recall, and F1;
- confusion matrix;
- accepted-prediction accuracy;
- local coverage after abstention;
- false-expansion and false-narrowing rates; and
- expected calibration error.

Span extractors report:

- exact character-span match;
- token precision/recall/F1;
- minimal-span accuracy;
- invalid-span rate;
- no-span accuracy; and
- accuracy conditional on acceptance.

Pair classifiers report:

- accuracy and macro F1;
- per-label metrics;
- hard-negative accuracy;
- polarity, quantifier, condition, and exception conservation subsets;
- accepted-prediction accuracy and coverage; and
- calibration error.

### Non-inferiority rule

For each decision kind, compare the local model and frozen LLM baseline against
the same gold examples. Promotion requires:

1. on examples accepted locally, local point accuracy is at least the LLM's
   accuracy on that exact subset;
2. the 95% paired-bootstrap confidence interval for
   `local_accepted_accuracy - llm_same_subset_accuracy` has a lower bound no
   worse than -1 percentage point;
3. every critical-label precision is at least the LLM's precision and at least
   99% conditional on local acceptance;
4. false expansion and false narrowing do not exceed the LLM baseline;
5. invalid response rate is zero by construction;
6. local accepted coverage is at least 90% for C01-C07 and 80% for E01-E05 and
   C08-C11; and
7. across the full held-out set, the hybrid local-plus-fallback point accuracy
   is at least the LLM-only point accuracy and its paired 95% confidence lower
   bound is no worse than -1 percentage point.

The one-point confidence allowance addresses finite-test uncertainty; it does
not permit a lower point estimate. If confidence intervals remain wide,
collect more held-out examples rather than declaring a tie.

### System-level gate

Replay complete design interviews and require:

- no new source-faithfulness failure;
- no changed deterministic assembly output when local and LLM decisions agree;
- at least the same final-contract accuracy against adjudicated contracts;
- clarification rate no more than five percentage points above baseline;
- no high-risk intent erasure; and
- a materially faster median and tail design latency;
- one completed design emits exactly one worker-facing prompt and one private
  validation manifest;
- the prompt contains every actionable resolved contract exactly once; and
- prompt and manifest obligation and verification-case references have exact
  parity.

## Latency and resource targets

### Reference CPU profiles

Benchmark on at least:

1. x86-64, 4 physical cores, AVX2, 16 GB RAM;
2. Apple Silicon baseline, 4 performance cores available, 16 GB unified
   memory; and
3. CI/container profile with 2 vCPUs and 8 GB RAM.

Record exact CPU model, instruction set, core/thread limits, runtime version,
power mode, and concurrent load. GPU results are reported separately and never
substitute for CPU qualification.

### Warm inference targets

Initial targets on the x86-64 reference CPU:

| Operation | p95 target |
| --- | ---: |
| Clause encoder plus all cached heads | 50 ms |
| Span encoder plus all five heads | 75 ms |
| One semantic pair | 50 ms |
| Batch of 16 candidate pairs | 200 ms |
| Common complete clause path excluding repository I/O | 500 ms |

Additional targets:

- cold model-service startup below 3 seconds;
- combined resident memory below 1 GB;
- combined quantized model artifacts below 300 MB;
- no network access during inference; and
- at least 10x lower median decision latency than the current LLM baseline.

These are qualification targets, not assumed facts. If a model misses them,
measure before changing architecture.

### Benchmark protocol

- Run 100 untimed warmups.
- Run at least 1,000 timed examples per operation.
- Report p50, p95, p99, mean, and standard deviation.
- Measure batch sizes 1, 4, 8, 16, and 32 for pair classification.
- Measure tokenizer time separately from model time.
- Measure cold load, first inference, warm inference, and peak RSS.
- Pin thread counts and report them.
- Store raw samples in a benchmark artifact.
- Compare end-to-end local routing, including serialization and cache lookup,
  against end-to-end provider latency.

## CPU optimization and packaging

### ONNX Runtime

Export production models to ONNX with fixed maximum sequence lengths and
dynamic batch dimensions. Apply transformer graph optimization, then evaluate
dynamic int8 quantization. ONNX Runtime recommends preprocessing and generally
recommends dynamic quantization for transformer models; its CPU runtime
supports common signed and unsigned int8 formats:
[ONNX Runtime quantization guidance](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html).

Use this sequence:

1. export fp32 ONNX;
2. verify logits against the PyTorch checkpoint within an explicit tolerance;
3. apply transformer graph optimizations;
4. benchmark optimized fp32;
5. apply dynamic S8S8 int8 first;
6. recalibrate temperatures and thresholds for quantized logits;
7. rerun every accuracy and abstention gate; and
8. try U8U8, reduced range, or per-channel quantization only when the reference
   CPU exhibits documented saturation or accuracy loss.

Hugging Face's Optimum examples provide a maintained sequence-classification
path for ONNX Runtime optimization and quantization:
[Optimum ONNX text-classification example](https://github.com/huggingface/optimum-onnx/blob/main/examples/onnxruntime/quantization/text-classification/README.md).

Quantization is accepted only when every semantic promotion gate still passes.
Smaller files and faster inference do not justify a critical-label regression.

### Runtime process

Run local models in one long-lived classifier service owned by Workrr. The
service:

- loads manifests and checks hashes at startup;
- memory-maps or loads ONNX files once;
- exposes only typed local IPC, not a public network endpoint;
- batches pair requests for one Procedrr snapshot;
- caches clause and token encodings within one work item;
- enforces maximum input lengths before inference;
- returns logits, calibrated probabilities, accepted value or abstention, and
  model revision; and
- has no repository write access.

In-process inference is allowed initially, but the model-provider interface
must not depend on it. A service boundary avoids repeated loads across CLI
invocations and permits memory/resource controls.

### Apple acceleration

ONNX Runtime CPU remains the portable baseline. A Core ML or Metal execution
provider may be added for Apple Silicon after CPU qualification. It must use
the same model manifest and pass output-equivalence, calibration, and accuracy
tests. No Apple-only artifact may become the sole production model.

## Training hardware

### CPU-only work

Ordinary CPU hardware is sufficient for:

- deterministic rules;
- TF-IDF/logistic baselines;
- dataset validation and splitting;
- most SetFit prototypes on small datasets;
- calibration;
- ONNX export and quantization;
- all production inference; and
- evaluation at modest throughput.

Full transformer fine-tuning on CPU is technically possible but not the
recommended repeatable path because iteration time will be excessive.

### Recommended GPU profiles

| Training job | Recommended minimum | Comfortable profile |
| --- | --- | --- |
| MiniLM clause multi-task | 8 GB CUDA/MPS memory | 12-16 GB |
| MiniLM five-head token model | 8 GB | 16 GB |
| MiniLM pair cross-encoder | 12 GB | 16-24 GB |
| ModernBERT-base fallback | 16 GB with gradient accumulation | 24 GB |
| Three-seed hyperparameter sweep | One 24 GB GPU sequentially | Multiple 16-24 GB GPUs |

Mixed precision may be used during training when supported, but retain an fp32
reference evaluation. Training scripts must also support gradient accumulation
so model correctness does not depend on a specific accelerator size.

### Hosted acceleration

If local GPU hardware is unavailable, use an ephemeral hosted GPU only for
training. Upload only the versioned training dataset permitted by its data
classification. Download checkpoints, optimizer metadata required for replay,
logs, and environment lockfiles; then destroy the worker. Production inference
and private repository lookup remain local.

## Model artifact contract

Each promoted bundle has `semantic-classifier-model-v1`:

```yaml
schema_version: semantic-classifier-model-v1
model_id: semantic-clause-minilm-v1
bundle_kind: clause | span | pair
backbone:
  source: microsoft/MiniLM-L12-H384-uncased
  revision: immutable-upstream-commit
  license: MIT
tasks:
  C03:
    decision_contract_revision: quantifier-v1
    labels: [one, some, every, unspecified]
    temperature: 1.17
    thresholds:
      every: 0.993
      one: 0.975
    margin: 0.20
dataset_revision: sha256:...
training_code_revision: git:...
runtime:
  format: onnx
  opset: exact-value
  quantization: dynamic-s8s8
  max_lengths: {clause: 128}
artifacts:
  model: {path: ..., sha256: ...}
  tokenizer: {path: ..., sha256: ...}
evaluation_report: {path: ..., sha256: ...}
supported_hardware: [x86_64-avx2, arm64]
```

Large model files do not belong in Git. Check in the manifest and evaluation
summary; store immutable model artifacts in a content-addressed artifact store.
Offline installation resolves exact hashes into the local Powdrr model cache.

## Runtime provider contract

Add a provider-neutral interface shaped like:

```python
class SemanticDecisionProvider(Protocol):
    def classify(self, request: SemanticDecisionRequest) -> SemanticPrediction: ...
```

`SemanticPrediction` contains:

```yaml
decision_kind: C03
status: resolved | unresolved
value: every | null
reason_code: null | classifier_abstained | unsupported_concept
probabilities:
  one: 0.001
  some: 0.001
  every: 0.997
  unspecified: 0.001
model_ref: semantic-clause-minilm-v1
input_fingerprint: sha256:...
latency_ms: 12.4
```

Workrr converts this into the common semantic-decision envelope and determines
fallback. The local model never selects the next provider.

### Provider routing

```text
deterministic rule resolved
  -> accept
deterministic rule no_decision
  -> local classifier
local classifier accepted
  -> accept
local classifier abstained
  -> configured Jev or planning-LLM fallback
fallback unresolved or high-risk disagreement
  -> human clarification / blocked capability
```

Use shadow mode before changing routing. Shadow predictions are stored but do
not affect contract assembly.

## Reproducibility and determinism

Training records:

- source commit;
- Python and dependency lockfile;
- CUDA/MPS/runtime versions;
- upstream model revision and hashes;
- dataset revision;
- exact split manifest;
- seeds;
- hyperparameters;
- hardware description;
- checkpoints and metrics; and
- calibration and quantization artifacts.

Inference fixes:

- tokenizer revision;
- ONNX model and runtime version;
- graph optimization level;
- thread counts;
- input truncation rules;
- temperatures and thresholds; and
- label maps.

Exact floating-point probabilities may vary slightly across hardware. Semantic
determinism is required: all supported hardware profiles must return the same
accepted label or abstention on the qualification suite. Any example whose
threshold decision changes across hardware must be forced to abstain by adding
a safety margin.

## Monitoring after promotion

Record for every local decision:

- decision kind and contract revision;
- model revision;
- accepted label or abstention reason;
- calibrated top probability and margin;
- latency and hardware profile;
- fallback provider and result, when used;
- later human correction or semantic-review finding; and
- input family fingerprint, excluding sensitive raw text from aggregate
  telemetry.

Monitor:

- accepted coverage by task and label;
- fallback and clarification rates;
- probability and margin distributions;
- disagreement with shadow LLM samples;
- human overturn rate;
- source-faithfulness failures;
- repository and vocabulary drift;
- p50/p95/p99 latency; and
- model-load failures.

### Drift triggers

Retraining review is required when any is true:

- accepted accuracy estimated from adjudicated samples falls below its gate;
- a critical-label precision lower confidence bound falls below 99%;
- fallback rate increases by 25% relative or five absolute points;
- input out-of-distribution score exceeds the calibrated threshold for more
  than 5% of a weekly sample;
- a classifier contract changes;
- supported language/domain distribution changes materially; or
- a new recurring confusion family reaches 25 adjudicated examples.

Do not continuously fine-tune production weights. Accumulate a new immutable
dataset revision, train a new model revision, and repeat promotion.

## Failure analysis taxonomy

Every wrong or abstained prediction receives one code:

```text
annotation_error
source_ambiguity
label_definition_gap
lexical_out_of_distribution
repository_domain_shift
negation_error
quantifier_error
condition_attachment_error
exception_attachment_error
span_boundary_error
candidate_evidence_insufficient
hard_negative_confusion
calibration_error
quantization_regression
hardware_variance
model_capacity_limit
```

Only `model_capacity_limit` justifies immediately trying a larger backbone.
Data, label, calibration, or evidence defects must be fixed at their own layer.

## Repository layout

Suggested implementation layout:

```text
classifier/
  README.md
  annotation-guides/
  dataset-schemas/
  manifests/
  configs/
    clause-minilm.yaml
    span-minilm.yaml
    pair-minilm.yaml
  scripts/
    build_dataset.py
    validate_dataset.py
    train_clause.py
    train_span.py
    train_pair.py
    calibrate.py
    export_onnx.py
    benchmark.py
    compare_baselines.py
  reports/
    checked-in-summary-only/
src/powdrr_lift/core/
  semantic_classifier_contract.py
src/powdrr_lift/workrr/
  semantic_classifier_provider.py
  local_classifier_runtime.py
tests/
  classifier/
```

Training dependencies belong in a separate optional dependency group so normal
Powdrr installation does not pull PyTorch, datasets, and training tooling.
Runtime installation should require only tokenizer support and ONNX Runtime,
subject to final packaging measurements.

## Implementation PR sequence

### PR 1: benchmark and data contracts

Implement:

- example, split, baseline, and evaluation-report schemas;
- annotation guide templates;
- dataset validators;
- grouped split compiler;
- current LLM baseline runner;
- paired metrics and bootstrap confidence intervals; and
- latency harness.

Gate:

- a small hand-adjudicated corpus runs end to end;
- leakage tests prove families cannot cross splits;
- baseline outputs and raw timings are replayable; and
- no runtime classifier routing changes.

### PR 2: deterministic resolvers and simple baselines

Implement:

- exact lexical rules;
- TF-IDF/logistic training;
- SetFit prototype training;
- common prediction artifact;
- calibration report; and
- rule precision/coverage reports.

Gate:

- every rule has 100% held-out precision on resolved examples;
- simple baselines establish per-task lower bounds; and
- no model is promoted.

### PR 3: local classifier runtime

Implement:

- model manifests and hash validation;
- content-addressed local cache;
- provider protocol;
- in-process or service runtime;
- shadow routing;
- telemetry artifacts; and
- fake-model contract tests.

Gate:

- a synthetic ONNX model can answer every response shape;
- stale revisions and hashes are rejected;
- shadow mode cannot alter decisions; and
- runtime works without network access.

### PR 4: clause classifier bundle

Implement:

- MiniLM multi-head training;
- answerability heads;
- task sampling;
- temperature calibration;
- abstention thresholds;
- ONNX export and quantization; and
- C01-C07 plus AC01/C08-source evaluation.

Gate:

- each promoted head independently passes its accuracy, coverage, critical
  precision, latency, and hardware-equivalence gates;
- failing heads remain on LLM routing; and
- accepted heads enter shadow mode only.

### PR 5: exact-span extractor

Implement:

- offset-based dataset conversion;
- five overlapping BIO heads;
- constrained decoding;
- exact-source round-trip checks;
- optional span-ranker bake-off; and
- CPU export.

Gate:

- exact character match equals or exceeds the LLM;
- invalid-span output is impossible;
- each role satisfies held-out support and coverage; and
- overlap fixtures pass.

### PR 6: semantic pair classifier

Implement:

- C08-C11 cross-encoder heads;
- repository hard-negative mining;
- source-conservation mutation families;
- candidate batching;
- calibration and abstention; and
- CPU export.

Gate:

- all pair-task gates pass on hard-negative and repository-shift subsets;
- candidate scores/ranks demonstrably do not leak into model input; and
- a batch of 16 candidates meets the latency target.

### PR 7: selective production routing

Implement:

- per-decision-kind promotion configuration;
- local-first routing for passing heads;
- fallback on abstention;
- sampled LLM shadow comparison;
- drift dashboards/artifacts; and
- rollback to LLM-only configuration.

Gate:

- complete design-flow replay is at least as accurate as LLM-only;
- common-path median latency is at least 10x faster;
- no critical intent error appears in the canary set;
- fallback and clarification rates remain within limits; and
- rollback requires configuration only, not a code deployment.

This routing changes only design-time semantic decisions. Regardless of which
classifier provider resolves them, the downstream design emits one
mini-SWE-agent prompt and does not select among coding-agent providers.

### PR 8: ModernBERT fallback only where justified

For any task that failed with MiniLM:

1. classify its failures using the failure taxonomy;
2. fix data/evidence/calibration issues first;
3. train ModernBERT-base only for remaining capacity-limited tasks;
4. export and qualify it on CPU;
5. compare its accuracy/latency frontier against keeping LLM fallback; and
6. promote only if the added local coverage justifies memory and latency.

This PR may be unnecessary. The plan explicitly prefers no larger model when
the small portfolio already meets the goal.

## End-to-end acceptance scenario

For `All data should pickle.` the qualified runtime should behave as follows:

1. Deterministic rules resolve polarity `required`, strength `should`, and
   quantifier `every`.
2. The clause model resolves disposition and behavior family or abstains.
3. The span model extracts exact `data` and `pickle` character spans.
4. Repository code retrieves data-population candidates.
5. The pair model evaluates each unresolved candidate relation in a batch.
6. Deterministic assembly binds a complete population receipt.
7. The source-predicate classifier returns `not_stated`; it must not invent
   semantic equivalence.
8. Existing accepted definitions are evaluated by the pair model one at a
   time, or the flow requests human clarification.
9. Every accepted local decision records model revision, probabilities,
   threshold, latency, and source fingerprint.
10. The final contract is identical to the contract produced by correct LLM
    decisions, because assembly is provider-independent.
11. Deterministic prompt compilation combines that contract with all other
    resolved contracts, repository facts, scope, and verification cases.
12. The design phase emits one immutable prompt and Workrr invokes
    mini-SWE-agent once with its exact `prompt` field.

The local path succeeds only if it preserves the deliberate unresolved result
at step 7. Faster confident invention is a regression.

## Explicit non-goals

- Do not train a generative local model to emit whole contracts.
- Do not train on unreviewed LLM outputs as truth.
- Do not use test data to choose backbones, thresholds, or seeds.
- Do not collapse conditions into quantifiers or non-goals into
  nonactionable instructions.
- Do not force a prediction to improve apparent coverage.
- Do not promote a model based only on aggregate accuracy.
- Do not require a GPU in the production runtime.
- Do not make Jev or another remote service a prerequisite for local
  classification.
- Do not turn classifier decisions, obligations, verification cases, or
  validation failures into multiple mini-SWE-agent prompts.
- Do not use OpenCode as a fallback after prompt compilation.
- Do not store private repository source in model artifacts or aggregate
  telemetry.
- Do not silently update weights, tokenizers, label maps, or thresholds.
- Do not adopt ModernBERT solely because it is newer.
- Do not let quantization bypass semantic evaluation.

## Completion criteria

This plan is complete when:

1. every classifier and extractor has a trained or deterministic provider;
2. each promoted local decision kind meets the frozen LLM non-inferiority gate;
3. high-risk accepted predictions achieve at least 99% held-out precision;
4. local coverage meets task-family thresholds and remaining cases fall back
   cleanly;
5. all production inference runs on the reference CPU profiles;
6. common-path decision latency is at least 10x lower than LLM-only;
7. model artifacts and datasets are reproducible and content-addressed;
8. provider replacement does not change Procedrr or semantic contract schemas;
9. design-flow replay finds no new intent loss or expansion; and
10. every completed design produces one deterministic mini-SWE-agent prompt,
    one complete private validation manifest, and no implementation repair or
    continuation prompts;
11. prompt and manifest obligation and case sets remain identical across
    qualified classifier providers; and
12. production monitoring can detect classifier drift and roll back to
    LLM-only design decisions without changing the single mini-SWE-agent
    implementation boundary.
