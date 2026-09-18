# Procedrr feature review prompts for OpenCode

These are two independent review passes for a feature pull request. They are
deliberately expressed as single decisions: each pass produces exactly one
typed JSON result and does not edit the worktree.

Replace the angle-bracket values before sending a prompt.

## Pass 1: specification completeness

```text
You are reviewing pull request <PR_NUMBER> in <REPOSITORY> at head <HEAD_SHA>.

This is a specification-completeness review. Do not edit files, approve or
merge the pull request, post comments, or propose unrelated redesigns.

Authoritative inputs:
- Feature specification: <FEATURE_SPEC_PATHS>
- Proposed-PR specification: <PROPOSED_PR_SPEC_PATHS>
- Base revision: <BASE_SHA>
- Head revision: <HEAD_SHA>

Read the complete authoritative specifications first. Then inspect the complete
base-to-head diff, the surrounding implementation and callers, relevant tests,
and the exact validation results. Build a checklist with one row for every
functional requirement, acceptance criterion, required edge/error case,
interface, migration, integration, documentation obligation, and validation
command in scope for this PR. Do not infer extra requirements from personal
preference or from later proposed PRs.

For every row, classify it as exactly one of: satisfied, partial, missing,
contradicted, or not_applicable. Every row must cite concrete implementation
evidence and test/validation evidence, or explain why the evidence is absent.
Treat a missing evidence mapping as a finding even when the test suite passes.
Findings must be caused by this PR and must not duplicate one root cause.

Return exactly one JSON object and no markdown:
{
  "decision": "complete" | "incomplete" | "blocked",
  "scope": {
    "feature": "<feature-id>",
    "proposed_pr": "<proposed-pr-id>",
    "base_sha": "<BASE_SHA>",
    "head_sha": "<HEAD_SHA>"
  },
  "criteria": [
    {
      "id": "<requirement-or-acceptance-id>",
      "status": "satisfied" | "partial" | "missing" | "contradicted" | "not_applicable",
      "implementation_evidence": [{"path": "<repo-relative-path>", "line": 0, "note": "<specific evidence>"}],
      "validation_evidence": [{"path": "<repo-relative-path-or-command>", "line": 0, "note": "<specific evidence>"}],
      "finding": "<required only when status is not satisfied or not_applicable>"
    }
  ],
  "summary": "<one concise explanation>"
}

The decision is complete only if every in-scope row is satisfied and every row
has concrete evidence. If the specifications or diff cannot be located, return
blocked with the exact missing input in summary and an empty criteria array.
``` 

## Pass 2: specification-justified change scope

```text
You are reviewing pull request <PR_NUMBER> in <REPOSITORY> at head <HEAD_SHA>.

This is an adversarial scope-justification review. Do not edit files, approve or
merge the pull request, post comments, or request a redesign merely because you
would implement it differently.

Authoritative inputs:
- Feature specification: <FEATURE_SPEC_PATHS>
- Proposed-PR specification: <PROPOSED_PR_SPEC_PATHS>
- Base revision: <BASE_SHA>
- Head revision: <HEAD_SHA>
- Completeness decision from pass 1: <PASS_1_JSON>

Read the authoritative specifications and the complete base-to-head diff. Audit
every changed file and every changed hunk, including product code, tests,
documentation, configuration, generated files, dependency changes, and
formatting-only edits. For each change, identify the smallest exact reason it
exists. A valid justification must be one of:

1. directly required by a feature or proposed-PR requirement;
2. required by an acceptance criterion or specified edge/error case;
3. a necessary compatibility, migration, or integration correction to deliver
   specified behavior; or
4. required validation evidence for the specified behavior.

Do not treat “useful,” “cleaner,” “future-proof,” or “while I was here” as a
justification. Do not flag necessary implementation detail when it is the
smallest change that satisfies an identified requirement. Challenge new
frameworks, abstractions, dependencies, control flow, and broad refactors only
when the specification does not require them or the repository contains a
smaller adequate mechanism. Every finding must cite the authoritative source
and the smallest changed line that exposes the unjustified scope.

Return exactly one JSON object and no markdown:
{
  "decision": "justified" | "unjustified" | "blocked",
  "scope": {
    "feature": "<feature-id>",
    "proposed_pr": "<proposed-pr-id>",
    "base_sha": "<BASE_SHA>",
    "head_sha": "<HEAD_SHA>"
  },
  "changes": [
    {
      "path": "<repo-relative-path>",
      "line": 0,
      "status": "justified" | "unjustified" | "unclear",
      "justification_type": "requirement" | "acceptance_criterion" | "compatibility" | "validation" | "none",
      "source_id": "<authoritative-requirement-id-or-null>",
      "evidence": "<specific diff and specification evidence>",
      "smallest_removal": "<minimal removal or narrowing when unjustified>"
    }
  ],
  "rejected_candidates": ["<candidate rejected because it was actually required>"],
  "summary": "<one concise explanation>"
}

The decision is justified only if every changed hunk has a valid mapping and no
unjustified or unclear change remains. If the authoritative specification or
complete diff cannot be located, return blocked with the exact missing input.
``` 

## Procedrr mapping

These prompts are intended to become two `DecisionContract` instances rather
than legacy workflow steps:

- Pass 1 uses `DecisionKind.CLASSIFY_ONE` with a completeness output schema and
  a dedicated review-complete transport action.
- Pass 2 uses `DecisionKind.CLASSIFY_ONE` with a scope-justification output
  schema and a dedicated scope-review-complete transport action.

The OpenCode output is review evidence for the decisions. It must not directly
edit code or decide transport actions such as `next_step`, `complete`, or
`retry`; the Procedrr runtime owns those transitions after validating the typed
result.

## Review-correction fixture

The flow must be tested against a change that produces real findings. Start with
a passing implementation fixture and apply two deterministic mutations in a
temporary review branch:

1. Remove or bypass one explicitly required edge/error behavior. This creates a
   specification-completeness finding.
2. Add one harmless-looking file, dependency, API, or refactor that has no
   requirement, acceptance criterion, compatibility need, or validation role.
   This creates a scope-justification finding.

The mutations must be part of the reviewed base-to-head diff. Do not tell the
reviewer which lines were mutated. The expected flow is:

```text
seed passing fixture
  -> apply bounded review mutations
  -> completeness decision: incomplete
  -> scope decision: unjustified
  -> repair decision: construct exactly one bounded repair plan
  -> runtime applies the repair plan
  -> deterministic validation
  -> repeat both reviews against the new head
  -> complete only when both decisions are clean
```

If a review finds no issue against the mutated fixture, the scenario fails: the
review prompt or evidence handoff is insufficient. If the repair cannot be
validated, the runtime must return a typed blocked result rather than silently
accepting the change.

## Pass 3: review correction

Run this as a separate OpenCode activation after both review decisions contain
actionable findings. It is a `DecisionKind.CONSTRUCT_ONE` decision. The model
constructs a repair plan; it does not select the transport action or directly
mutate the repository.

```text
You are preparing one bounded repair for pull request <PR_NUMBER> at head
<HEAD_SHA>. Do not edit files, run commands, approve or merge the pull request,
or repair findings that are merely preferences.

Inputs:
- Feature specification: <FEATURE_SPEC_PATHS>
- Proposed-PR specification: <PROPOSED_PR_SPEC_PATHS>
- Completeness review decision: <PASS_1_JSON>
- Scope review decision: <PASS_2_JSON>
- Current repository status and diff: <REPOSITORY_STATE>

Select only actionable findings caused by the current diff. Prefer one root
cause and the smallest repair that makes the implementation satisfy the
authoritative specification. The repair may add missing behavior, add focused
validation, remove unjustified scope, or narrow an unjustified change. It must
not introduce a new requirement, redesign unrelated code, or edit review
artifacts. Every edit must cite the finding and authoritative source it
addresses.

Return exactly one JSON object and no markdown:
{
  "decision": "repair" | "no_repair" | "blocked",
  "repair": {
    "finding_ids": ["<finding-id>"],
    "summary": "<one root cause>",
    "edits": [
      {
        "path": "<repo-relative-path>",
        "operation": "replace" | "add" | "remove",
        "start_line": 0,
        "end_line": 0,
        "text": "<replacement text or empty string>",
        "source_id": "<requirement-or-acceptance-id>"
      }
    ],
    "validation_commands": ["<commands already declared by the repository>"],
    "postconditions": ["<specific review finding that must disappear>"]
  },
  "summary": "<why this is the smallest justified repair>"
}

Return no_repair only when all findings are already resolved or rejected with
concrete specification evidence. Return blocked when the specification, finding
identity, edit target, or declared validation is unavailable. The runtime owns
the edit operation, validation, new diff fingerprint, and re-entry into Pass 1;
the model must not return `next_step`, `complete`, or `retry`.
```

The correction loop is bounded by the workflow resource limits. A second review
must use the new head fingerprint and fresh validation evidence; it may not reuse
the prior clean result. Publication remains blocked while either review returns
`incomplete`, `unjustified`, `blocked`, or an unresolved repair postcondition.
