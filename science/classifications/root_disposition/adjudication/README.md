# Root disposition double-label batch

This is a **human annotation packet**, not a completed gold set. Two independent
reviewers must label the same 125 examples. The packet is stratified by the
existing LLM label and sampled only from the pilot model's three held-out
repository families. That keeps the batch useful for evaluating the pilot
without putting its source repositories into training.

## Reviewer instructions

Give each reviewer only their own `annotator_a.jsonl` or
`annotator_b.jsonl`. Do not share answers or consult the previous LLM labels
until both sheets are complete. The files contain only an opaque review ID and
the exact proposition shown to the classifier.

For each item, fill `label` with one of the values below. If selecting
`unresolved`, also fill `unresolved_reason`. Give a confidence rating for the
label itself (`high`, `medium`, or `low`) and add a short note when it helps
explain a boundary decision. Preserve the review ID and do not edit the
proposition.

## Rubric

Classify the exact proposition in isolation, as the initial root-disposition
pass does. Do not use later workflow stages, repository code, or unstated
context to infer a more specific role. A proposition can describe a desired
behavior even when phrased as a statement rather than an imperative. Do not
classify its polarity, strength, quantifier, or implementation details here.

- `entity`: names or defines a product concept, domain object, actor, type, or
  component. Example: “A Report is a domain record.”
- `feature`: describes a product capability or behavior. This is the default
  for a concrete product action that is not mainly an API contract or a rule
  that must hold across a population. Example: “Users can export reports.”
- `interface`: specifies a boundary contract: public API shape, signatures,
  inputs, outputs, callbacks, or how callers and the product communicate.
  Example: “Expose `get_state_data(state)`.”
- `invariant`: states a rule or property that must hold generally across a
  population, operations, or lifecycle. A universal rule is an invariant even
  when it also describes behavior. Example: “Every response has an ID.”
- `guidance`: expresses a preference or recommendation without establishing a
  definite product behavior requirement. Example: “Prefer immutable defaults.”
- `non_goal`: explicitly excludes or prohibits product behavior. Example: “Do
  not add CSV export.” This is distinct from a workflow or delivery instruction.
- `nonactionable`: concerns only process, delivery, repository handling, or
  tools, with no product semantics. Example: “Run the unit tests before
  submitting.”
- `formatting_artifact`: is an incomplete extraction fragment or structural
  debris that does not express a proposition, such as an isolated list number
  or heading stub. This is distinct from a meaningful process instruction.
- `context`: gives background, motivation, or a problem statement without
  requesting or defining product behavior. Example: “Without a lifecycle,
  callers manage values manually.”
- `unresolved`: the proposition does not support one defensible label, is
  underspecified, or appears to mix incompatible roles after atomic splitting.
  Choose `source_ambiguous`, `source_underspecified`, or
  `unsupported_concept` as the reason.

### Boundary rules

- Product meaning takes precedence over delivery wording. “Do not change the
  public callback signature” is `non_goal`; “Open a pull request” is
  `nonactionable`.
- Prefer `interface` when the proposition is specifically about an externally
  visible contract. Prefer `feature` for what the product does through that
  contract.
- Use `invariant` for a general rule, not merely because a sentence contains
  “all,” “every,” or “always.” The rule must describe a property expected to
  hold across cases.
- Use `guidance` only when the source leaves the behavior as a preference or
  recommendation. Words like “should” alone do not decide the disposition.
- Use `context` for a factual explanation of a problem; use `feature` when the
  text asks for the system to change or provide behavior.
- Do not resolve ambiguity by looking at the LLM label. Mark `unresolved` and
  explain the ambiguity instead.

When both sheets are complete, run the comparison utility:

```bash
uv run python science/classifications/root_disposition/adjudication/compare_reviews.py
```

It validates both sheets, reports raw agreement and Cohen's kappa, fills
`adjudication.jsonl` for resolved labels where both reviewers agree, and writes
`adjudication-worklist.jsonl` for every disagreement or `unresolved` response.
A third reviewer should resolve each worklist item and record the final label,
reason if unresolved, rationale, and their reviewer ID in both the worklist and
`adjudication.jsonl`. Do not overwrite the original reviewer answers. The
`selection_key.jsonl` maps opaque review IDs to source rows and prior teacher
labels; keep it with the coordinator and do not give it to reviewers.

## Regenerate the packet

From the repository root:

```bash
uv run python science/classifications/root_disposition/adjudication/prepare_batch.py \
  --output-dir /tmp/root-disposition-review-batch
```

The fixed seed and source-family list make the sample reproducible. The script
records the source dataset hash in `batch-manifest.json` and refuses to
overwrite an existing annotation packet. If the source dataset changes,
generate a new packet in a separate directory and review it before sharing.
The held-out source families are recorded in
`holdout-families.json` and should remain excluded from future training when
using this batch as an evaluation set.
