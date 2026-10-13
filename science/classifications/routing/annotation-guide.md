# Routing annotation guide

## Task

Label the exact target proposition using only the provided target, local source
context, and scope relations. The source-family and task identifiers, prior
classifier labels, and other annotator's answers are not part of the review
input. Do not use implementation patches, test cases, solutions, or later
generated design content.

First decide what the target says in its supplied passage. Does it report an
existing circumstance, request or define desired product behavior, prohibit
product behavior, or describe workflow? Neighboring text may resolve a
reference or heading; it must not donate its obligation to this target. If the
source contains independent meanings, label the atomic target provided. Do not
split it yourself during this pass; flag a mixed unsplittable target in the
rationale and use `unclear` only if no single route is defensible.

## Routes

| Label | Use when | Examples |
| --- | --- | --- |
| `context` | The target reports present behavior, a limitation, motivation, or other existing circumstance without requesting a product change. | “The cache is currently shared by every worker.” “The parser currently cannot retain offsets.” |
| `include` | The target specifies positive product behavior, a product definition, interface, invariant, or implementation guidance. | “The cache must be shared by every worker.” “On exit, the data is removed.” “Start each session with an empty cache.” |
| `include_prohibition` | The target explicitly rules out product behavior or excludes it from product scope. | “Do not add automatic retries.” “The parser must not discard offsets.” |
| `exclude` | The target is a process, delivery, repository, or schedule instruction without product semantics. | “Start by running the unit tests.” “The release will be reviewed on Friday.” |
| `unclear` | Even with the supplied passage, the target has no defensible route without guessing. | “The cache is shared.” with no heading or surrounding clarification. |

## Cue words are evidence, not rules

Present-state “is” sentences often give context, but “On exit, the data is
removed” is a passive requirement, and “A session is a container for one
user's state” is a product definition. `must`, `should`, `will`, and imperative
verbs such as `start` often signal product behavior, but “Start by running the
tests” is process. “The release will be reviewed Friday” is a schedule. Read
the target's role in the passage instead of applying a word lookup.

Distinguish a reported deficiency from a prohibition: “The system currently
does not retry” is context; “The system must not retry” is a prohibition.
Distinguish a prohibition from a required rejection: “Reject malformed input”
is positive product behavior and is `include`.

## Review record

For every row:

- Choose one route.
- Record confidence as `high`, `medium`, or `low`.
- Give a short rationale tied to the target's role.
- Add one or more exact evidence quotes from the supplied input. Use an empty
  list only when no short quote can support the decision.
- If choosing `unclear`, select `source_ambiguous`, `source_underspecified`, or
  `unsupported_concept` and explain the ambiguity. Reviewer uncertainty alone
  is not a reason to label a source `unclear`; use low confidence and describe
  the rubric question instead.

Review independently. Do not compare notes before completing the sheet. A
human agreement is a reviewed label; disagreements, low-confidence cases, and
all assistant-model suggestions need separate human adjudication before they
enter the gold set. Keep original reviewer records unchanged.

## Scope and data handling

The pilot packet is development data. Its families and the old Jev challenge
families must never be reported as a fresh test set. Do not share the
coordinator-only `selection_key.jsonl` with annotators. The key contains source
identifiers and hidden silver-label suggestions. Review only
`reviewer_a.jsonl` or `reviewer_b.jsonl` and return that completed sheet to the
coordinator.
