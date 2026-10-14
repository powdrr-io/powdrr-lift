# Pilot question prompts

These are draft prompts for the two passes in the
[experiment protocol](README.md). Each call should handle one instruction
clause or one frozen obligation. The caller validates the JSON schema and
source references; a schema-valid answer is not evidence that its judgment is
correct.

## Pass A: obligation and evidence plan

**Question:** What independently checkable obligations does this instruction
clause impose, and what evidence could establish each one?

**Context:** Original instruction, exact clause text and source span, and
applicable repository instructions. Do not include candidate code, coding-agent
summaries, test results, solution patches, or validation patches.

**Instructions:**

- Split independent outcomes, prohibitions, interfaces, scope limits, and
  process requirements into separate obligations.
- Preserve the source's normative strength and conditions. Do not strengthen
  “should” into “must,” infer unspecified defaults, or turn positive behavior
  into an unstated rejection policy.
- For each obligation, ask one focused question about an observable fact after
  the coding run. Prefer a concrete state, action, and observation when the
  instruction defines behavior.
- Select the narrowest evidence route that could answer the question. Choose
  `structural_diff`, `executable_check`, `test_result`, `repository_metadata`,
  `semantic_review`, or `not_checkable`.
- State what evidence would support `pass` and what observation would establish
  `fail`. Use `unknown` when the instruction or available evidence cannot
  define either outcome. Do not fabricate a test or claim that a test exists.
- Keep the evidence plan independent of any presumed implementation design.

**Output:**

```json
{
  "obligations": [
    {
      "id": "clause-id-1",
      "source_span": {"start": 0, "end": 0},
      "kind": "behavior",
      "text": "One obligation, faithful to the instruction.",
      "question": "One answerable question about the completed work?",
      "evidence_route": "executable_check",
      "pass_evidence": "Observable evidence that would establish compliance.",
      "fail_evidence": "Observable evidence that would establish noncompliance.",
      "unknown_when": "What cannot be determined from the available evidence."
    }
  ],
  "unresolved_source_questions": []
}
```

## Pass B: candidate-bound evidence review

**Question:** Does the supplied evidence establish that this one frozen
obligation was met for this candidate revision?

**Context:** The original instruction and clause, one frozen obligation, the
baseline and candidate identifiers, relevant candidate diff or repository
metadata, and verification output relevant to the selected evidence route.
Do not include the coding agent's summary, self-assessment, or verdicts for
other obligations.

**Instructions:**

- Judge the obligation as written against its source. Do not repair or reinterpret
  the obligation during review.
- Use only evidence supplied for this candidate. Cite evidence IDs or exact
  paths and command-result IDs; do not cite a test name without inspecting its
  assertions or a command without its result.
- A structural change alone does not establish behavior. A test pass establishes
  only the behavior its assertions observe. Missing evidence is `unknown`, not
  `pass`.
- Return `fail` only when the evidence contradicts the obligation or a required
  check failed. Return `unknown` when the relevant evidence is absent, stale,
  incomplete, or inconclusive.
- Do not infer that a requirement is satisfied because the agent says it is, or
  because unrelated tests pass.

**Output:**

```json
{
  "obligation_id": "clause-id-1",
  "candidate_revision": "revision-id",
  "outcome": "unknown",
  "evidence": [],
  "explanation": "Why the cited evidence does or does not answer the question."
}
```

Allowed `outcome` values are `pass`, `fail`, and `unknown`. The caller rejects
missing, duplicated, stale, or unrecognized evidence references and computes
the aggregate outcome deterministically.
