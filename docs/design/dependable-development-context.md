# Dependable Software Development Context

## Purpose

This document explains the product vision shared by Structrr, Procedrr, and
Workrr. It defines why the three systems exist, which reliability problem they
solve together, and the principles that constrain their design.

The companion documents are:

- [Structrr context language specification](structrr-context-language-specification.md)
- [Structrr, Procedrr, and Workrr integration](structrr-procedrr-workrr-integration.md)
- [Context system implementation plan](../plans/dependable-context-system-implementation.md)

## Problem

Source code records what a system does now, but it incompletely records why it
does it, which alternatives were rejected, what must remain true, how the code
relates to product concepts, or which future changes are already intended.
Issue trackers and design documents contain some of this information, but they
are generally prose-oriented, weakly connected, difficult to rebase, and only
available to an agent when someone remembers to retrieve them.

LLMs amplify this weakness. They can make locally plausible changes while
missing a decision made months earlier, a cross-cutting constraint, an
in-progress proposal, or a relationship to code outside the immediate prompt.
Larger prompts do not solve the problem: they increase noise while still
providing no guarantee that the applicable information was included.

The system must therefore make project memory structured, versioned,
queryable, and automatically applicable at the moment of judgment.

## Product thesis

Structrr complements source code with a semantic history and model of the
software product:

- the past is a sequence of explicit entity and relationship changes with
  provenance and rationale;
- the present is a materialized effective design and its source bindings; and
- the future is a set of versioned proposal overlays over an exact design
  revision.

This structure permits semantic rebasing, impact analysis, targeted context
delivery, and alignment review. The objective is not comprehensive project
documentation. It is dependable decision support: every consequential change
should be intentional, explainable, and consistent with current product
direction.

## Three-system model

### Structrr owns product meaning

Structrr describes product entities, relationships, requirements, decisions,
invariants, guidance, proposals, source bindings, and assurance applicability.
It answers what is true, why it is true, what is proposed, and what knowledge
applies to a product decision.

### Procedrr owns process meaning

Procedrr describes bounded LLM-assisted procedures. It owns typed decisions,
control flow, operation references, input/output bindings, limits, and static
guarantees. A procedure may require Structrr context by typed reference, but it
does not copy or reinterpret product truth.

### Workrr owns composition and execution

Workrr binds a compiled procedure to current product, source, and execution
state. It requests Structrr context, composes model inputs, invokes providers,
mediates operations, records observations and evidence, and re-resolves
context whenever execution changes the relevant world.

The governing invariant is:

> Every consequential LLM decision occurs inside a compiled Procedrr decision,
> receives a current and complete Structrr context packet, and is mediated and
> recorded by Workrr.

## Context is compiled, not remembered

Context delivery is mandatory runtime behavior. The model does not need to
remember that context exists or decide whether to call a retrieval tool.

```text
Procedrr decision boundary
  -> Workrr builds a ContextRequest
  -> Structrr resolves an exact design/source revision
  -> Structrr returns ContextPacket + ContextCoverage
  -> Workrr composes the model activation
  -> model proposes a typed decision
  -> Workrr executes or records the result
  -> observed changes trigger context re-resolution
```

Structrr supplies the minimum mandatory context. Optional exploration remains
available through query tools, but correctness cannot depend on the model
using them.

## Decision-time context

Context is resolved before proposal authoring, planning, consequential source
edits, dependency or interface changes, review responses, completion, and
merge. Resolution begins from concrete anchors such as:

- procedure, step, phase, and decision kind;
- work item and proposal identifiers;
- affected entities and relationships;
- paths, languages, symbols, and source bindings;
- proposed operation and arguments;
- open findings, obligations, and observed changes; and
- exact product, policy, proposal, and source revisions.

A context lens declares which categories are required for a decision kind.
Structrr traverses only relationships with relevance semantics for that lens,
then returns an explainable and token-bounded packet. Every included item says
why it applies and which relationship path selected it.

## Source and design are linked but distinct

Structrr must know where product meaning is realized in source without
becoming a duplicate source model. Stable entity-to-source bindings identify
definitions, implementations, consumers, tests, schemas, configuration,
documentation, generators, and generated artifacts.

Language-aware indexing resolves those bindings and observes source
relationships. Declared bindings represent design intent; observed bindings
provide evidence and drift detection. Paths and spans are useful locations,
but stable binding and entity identifiers survive movement and rename.

This enables a source diff to produce an observed semantic impact, which can
be compared with the proposal's declared product impact.

## Semantic proposals and rebasing

A proposal is a semantic patch against an immutable design snapshot. It
records the context and field-level assumptions it read, the entities and
relationships it intends to write, and its expected impact.

Rebasing is a three-way semantic operation:

```text
base design + upstream design delta + proposal semantic patch
```

Unrelated changes rebase automatically. Identity-preserving movement can be
remapped mechanically. Changes to read assumptions, applicable constraints,
or overlapping writes produce a targeted rebase report. Only affected
decisions are reopened.

At merge time the same process includes source:

```text
current design + rebased intended design delta
current source + branch source delta
intended impact versus observed impact
```

Merge readiness requires current context, successful semantic rebases,
design/source correspondence, and fresh evidence.

## Cross-cutting concerns without tool duplication

Structrr describes why and when a validation or governance capability applies.
It references an externally implemented tool target; it does not reproduce the
Makefile, CI workflow, scanner policy, or package-manager script.

```text
Structrr: Python changes require python-validation.
Procedrr: Completion requires applicable validation evidence.
Workrr: Resolve and invoke the referenced target.
Make/CI: Define what the target actually runs.
```

This same model covers linters, tests, security scanners, compliance reviews,
license checks, migrations, and approvals. Evidence is bound to the source,
configuration, and tool revision it validated and becomes stale when relevant
inputs change.

## Design principles

1. Product meaning is represented once and referenced by stable identifiers.
2. Current truth is derived from versioned history, not whichever prose is
   retrieved first.
3. Applicability is deterministic; semantic similarity may rank context but
   must not activate mandatory constraints.
4. Context is pushed at decision boundaries; retrieval tools are supplemental.
5. LLMs propose semantic choices; deterministic compilers own document shape.
6. External development tools remain authoritative for their execution.
7. Every decision records the context revision that informed it.
8. Rebase and review reopen only materially affected decisions.
9. Source observations never silently overwrite declared product meaning.
10. Missing required context is a typed failure, not an invitation to guess.

## Success criteria

The system succeeds when it can demonstrate that:

- required product context is recalled at the relevant decision boundary;
- irrelevant context remains bounded;
- stale proposals are classified by semantic impact;
- source changes map to declared or newly discovered product impact;
- applicable validation and governance requirements are identified without
  duplicating their executable definitions;
- reviewers can trace every finding to current product meaning and provenance;
- agents update Structrr through validated semantic operations; and
- merged code corresponds to a current, accepted Structrr design delta.
