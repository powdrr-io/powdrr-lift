# Agent Platform Expansion

## Purpose

This document captures the next-stage design for `powdrr-lift` as a platform
for installable skills, specification documents, synthesis workflows, and
relationship-aware context lookup across supported coding agents.

The goal is not just to store context, but to provide a coherent system that
can:

- specify and validate system, architecture, implementation, and proposed-PR
  documents
- synthesize new documents from parent state plus change diffs
- review documents for inconsistency, weak links, and missing constraints
- answer context questions using a graph of code, features, decisions,
  requirements, and entities
- keep the CLI and MCP surfaces aligned across macOS, Windows, and Linux

## Core Document Types

The platform should support these specification families:

- `system`
- `architecture`
- `implementation`
- `proposed-pr`

All of those specifications live under `docs/specs/<work-item-name>/` and use
the shared `https://powdrr.io/schemas/specification-v1` schema.

Each of these document types needs two representations:

- `current state`: a complete and coherent version of the document
- `diff`: a parent document plus adds, removes, and changes that can be
  applied to produce the next current state

The diff format should behave like git: apply the diff to the parent current
state, then validate the result to produce the next current state.

The platform should also support an `issues` document type that records known
incoherence explicitly when a change should not cascade through the whole
system.

## Skill Surface

The design assumes these installable skills are part of the package:

- `specify-system`
- `specify-architecture`
- `specify-implementation`
- `specify-prs`
- `implement-pr`
- `create-pr-changelog`
- `synthesize-implementation`
- `synthesize-architecture`
- `synthesize-system`
- `review-pr`
- `review-implementation`
- `review-architecture`
- `review-system`

Each `specify-*` skill and `create-pr-changelog` should have:

- an endpoint that creates the correct template with instructions
- an endpoint that validates the filled template

Each `synthesize-*` skill should have:

- an endpoint that produces the start state for a parent branch
- an endpoint that produces the new state for a PR
- an endpoint that produces the diff between those states

The review skills should produce diffs and then inspect multiple layers of the
system to identify inconsistencies, weak assumptions, missing references, and
other review concerns.

## Context and Graph Queries

The platform should expose context endpoints that answer questions about:

- code used together
- code updated together
- code linked to features
- code linked to decisions
- code linked to requirements
- code linked to entities
- the intent attached to a line, file, or region

These endpoints should use the relationship graph produced from changelogs and
specifications, not only the raw source tree.

## Validation Principles

Validation should always enforce graph coherence for the active specification
layer. When a change is intentionally inconsistent, the platform should support
`issues` documents so the mismatch is recorded rather than hidden.

The intended flow is:

1. generate the appropriate template
2. fill the template
3. validate the completed document
4. fix violations and repeat until clean

## Platform Constraints

- The CLI and MCP must work on macOS, Windows, and Linux.
- Installation should be a single command per platform.
- On macOS, Homebrew should provide the obvious one-command install path.
- The same authoritative content should drive all supported agent adapters.

## Open Design Questions

- Which document families should be treated as first-class current-state vs
  diff artifacts at launch?
- What is the minimum graph required to answer code-context questions well?
- Which review findings should become hard validation errors versus advisory
  review output?
- How much architecture churn should be introduced now versus deferred to the
  next pass?
