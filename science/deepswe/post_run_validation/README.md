# Post-run instruction validation experiment

This track asks whether instruction-focused questions can produce a useful,
independent review plan for checking a coding agent's completed work. It is
separate from the prompt-generation experiments: its output is a set of
evidence requests and verdicts, not implementation guidance or a rewritten
coding prompt.

See [EXPERIMENTS.md](EXPERIMENTS.md) for the staged study plan, comparison arms,
controls, scoring, and proposed decision gates. The question templates are in
[PROMPTS.md](PROMPTS.md). A narrow Koota proof of concept is recorded in
[RESULTS.md](RESULTS.md), with a GraphQL case in
[GRAPHQL_RESULTS.md](GRAPHQL_RESULTS.md).

## Experiment question

Given only the original instruction, can a reviewer derive a complete set of
small, independently checkable obligations? After the implementation, can a
second pass assess each obligation against the candidate diff and verification
evidence without relying on the coding agent's summary or rationale?

The first pilot should compare three ways to make the review plan:

1. A broad instruction-level question: “Was the instruction followed?”
2. A direct checklist of acceptance criteria.
3. Atomic questions that identify an obligation, ask what observable evidence
   would establish it, and route that evidence to a suitable checker.

The third arm is informed by the science work in
[`acceptance_templates`](../acceptance_templates/README.md). That work found
that decomposing criteria into assertions helped expose unsupported behavior,
while asking a model to approve its own proposed criterion did not reliably
fix semantic errors. A question or a citation is a review aid, not proof.

## Available pilot material

The acceptance-template experiment already contains four original instructions,
production instruction analyses, generated criteria, and agent-authored
reference validations:

- `science/deepswe/acceptance_templates/data/inputs/`
- `science/deepswe/acceptance_templates/data/references/`
- `science/deepswe/acceptance_templates/runs/production-routed-v1/`

For example, the Koota task's reference `v004` says entity snapshot data and
relation store data are deep copies, with nested mutation unable to change the
other side. A focused validation question could be:

> Given a registered data trait containing a nested object, what values remain
> in the snapshot after the entity's nested value is changed? What values remain
> in the entity after the snapshot's nested value is changed?

These artifacts can evaluate obligation discovery and checklist coverage. They
do **not** include a saved coding-agent candidate diff or test transcript, so
they cannot establish post-run verdict accuracy. The first end-to-end pilot must
capture those artifacts for the same tasks before it can make that claim.

## Proposed two-pass protocol

### Pass A: derive and freeze obligations

Input: original user instruction and any applicable repository instructions.
Do not provide the candidate diff, agent summary, tests, solution patch, or
validation patch to this pass.

For each atomic obligation, record:

- a stable ID and exact source span;
- obligation kind: behavior, interface, prohibition, scope, process, or
  verification instruction;
- one plain question that could be answered about the completed work;
- the evidence route: structural diff, executable check, test result, repository
  metadata, targeted semantic review, or not currently checkable;
- what evidence would count as pass, fail, or unknown.

Keep independent propositions separate. For example, “throws on destroyed
entities or unknown registry keys” becomes separate checks for the destroyed
entity case and the unknown-key case. Preserve unresolved wording as unknown;
do not invent implementation requirements to make it testable.

### Pass B: inspect the completed work

Bind the frozen obligations to the candidate revision and review one obligation
at a time. Give the reviewer the original instruction, relevant baseline and
candidate evidence, and relevant test outputs. Do not show the coding agent's
claims or other obligation verdicts. Return `pass`, `fail`, or `unknown`, with
evidence references and a short explanation. Derive aggregate status in code:
any fail means fail; otherwise any unknown means unknown; only all-pass means
pass.

The existing Structurr actual-diff and invariant-review packets are suitable
evidence sources for changed behavior. A passing structural diff check does not
prove behavior. A passing test result proves only the assertions that test
actually makes. Instructions about branch, commit, scope, or requested checks
need their own repository metadata or command evidence.

## Evaluation

Use the existing reference validations to score whether Pass A represented
instruction-required behavior. Report reference coverage, unsupported or
invented obligations, obligation count, and abstentions. Preserve the existing
reference provenance: these labels are agent-authored and are not human gold.

For Pass B, construct blinded candidate cases with known omissions and scope
violations, then compare verdicts with the planted facts and independently
reviewed evidence. Include positive controls where the implementation is
correct. Report missed violations, false failures, false passes, unknown rate,
and review cost. A reviewer that catches seeded mistakes but fails positive
controls is not ready to gate work.

The first end-to-end run should use at least one task with a saved candidate
revision, baseline revision, complete diff, and verification transcript. Keep
all four artifacts fingerprinted and tied to the same task and candidate. Do
not treat prompt-generation scores, test names, or the coding agent's own report
as evidence of compliance.

## Current status

The protocol and prompts are drafted. Focused Koota and GraphQL proof of
concepts found validation gaps in saved reference-task artifacts; see
[RESULTS.md](RESULTS.md) and [GRAPHQL_RESULTS.md](GRAPHQL_RESULTS.md). No live
coding-agent run has been reviewed, and no blinded reviewer comparison has
been run yet.
