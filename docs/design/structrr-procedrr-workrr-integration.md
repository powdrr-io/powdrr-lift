# Structrr, Procedrr, and Workrr Integration

## Purpose

This document defines how Structrr product context, Procedrr procedures, and
Workrr execution compose without sharing authority or creating circular
implementation dependencies.

See the [system context](dependable-development-context.md), the
[Structrr language specification](structrr-context-language-specification.md),
and the [implementation plan](../plans/dependable-context-system-implementation.md).

## Authority boundaries

| System | Owns | Does not own |
| --- | --- | --- |
| Structrr | Product meaning, history, proposals, source bindings, applicability, semantic rebase and review | Procedure control flow, provider behavior, tool command implementation |
| Procedrr | Bounded control flow, typed decisions, operations, bindings, limits, static guarantees | Product truth, current applicability, tool implementation, mutable execution state |
| Workrr | Runtime composition, context timing, provider calls, operation mediation, observations and evidence | Structrr semantics, Procedrr validation rules, external tool internals |
| Make/CI/tools | Commands, configuration, scanner behavior, executable validation | Product applicability and LLM context composition |

Workrr coordinates authorities; it MUST NOT replace them.

## Dependency structure

The systems communicate through immutable versioned contracts:

```text
                 shared contracts
                 /              \
       Structrr compiler      Procedrr compiler
                 \              /
                       Workrr
                         |
                  operation adapters
                         |
                   Make / CI / tools
```

Procedrr source documents MAY contain Structrr resource references and context
lens identifiers. Procedrr code SHOULD depend only on the reference contract,
not Structrr storage or query implementations. Structrr MUST NOT import Workrr
or Procedrr runtime code.

## Shared contracts

The integration surface consists of:

- `ResourceRef`: typed identity for product and procedure resources;
- `DesignRevision`, `PolicyRevision`, and `SourceRevision`;
- `DecisionBoundary`: current Procedrr decision metadata;
- `ContextRequest`, `ContextPacket`, and `ContextCoverage`;
- `SemanticPatch`, `ProposalDependencies`, and `RebaseReport`;
- `SourceImpact` and `AlignmentReport`;
- `ToolRequirement`, `ToolResolution`, and `EvidenceRecord`;
- `DecisionRecord`; and
- `MergeCertificate`.

Contracts MUST be provider-neutral and serialization-stable.

## Procedrr references to Structrr

Procedrr declares requirements, not retrieved content:

```yaml
kind: judge
decision:
  kind: construct_one
  subject: implementation approach
  context_bindings:
    - input:proposal
    - input:affected_entities
  context:
    provider: structrr
    lens: implementation-decision
    subjects:
      entities: binding:affected_entities
      proposal: binding:proposal
    coverage: required
```

Compilation validates that the reference shape and binding names are valid.
Workrr resolves the lens against a particular Structrr service and revision at
runtime. Procedrr does not embed current decisions, entity descriptions, or
tool lists in its compiled workflow.

An operation can use the same pattern:

```yaml
kind: operation
name: satisfy-applicable-validation
arguments:
  requirements: context:applicable_tools
```

The operation consumes a typed context output. It does not discover product
policy independently.

## Decision activation protocol

Before every Procedrr `JudgeNode`, Workrr performs this protocol:

1. Read the compiled decision and its context declaration.
2. Combine declared bindings with current execution state and latest
   observations to construct a `ContextRequest`.
3. Resolve aliases and bind `current` references to exact revisions.
4. Ask Structrr to compile a packet and coverage result.
5. Refuse activation when required coverage is incomplete.
6. Compose the Procedrr prompt, typed state, latest observation, and Structrr
   packet within the activation token budget.
7. Invoke the provider and validate the dedicated typed response.
8. Record a `DecisionRecord` binding the response to the packet fingerprint.

The model MAY request optional expansion, but it cannot remove mandatory
context or change the revision used by the activation.

## Context request construction

Workrr derives anchors from three sources.

### Static procedure bindings

- work item and proposal;
- declared entities and relationships;
- requested decision kind;
- phase, persona, and expected outputs; and
- context lens selected by the procedure.

### Runtime state

- active node and call stack;
- prior semantic decisions;
- open findings and obligations;
- resolved tool requirements;
- current source/design revisions; and
- current resource and activation limits.

### Proposed or observed effects

- operation name and arguments;
- affected paths, symbols, and languages;
- source diff and source bindings;
- dependency or schema changes; and
- newly discovered product entities.

Workrr MUST re-resolve context when an action adds anchors that can change
applicability.

## Prompt composition

A model activation contains four authoritative sections:

```text
P: compiled Procedrr decision and allowed output
S: bounded current execution state
C: Structrr context packet
O: latest observation or diagnostic
```

Historical transcript MAY be supplied for conversational continuity, but it
MUST NOT be the only source of required product context. Workrr should order
mandatory constraints before optional explanatory history and preserve stable
resource IDs in compaction.

## Static and dynamic context

Workrr requests static context when a workflow starts or a plan is compiled.
This packet identifies known requirements, product entities, decisions,
invariants, concurrent proposals, and validation capabilities.

Workrr requests dynamic context before and after consequential operations. A
source edit that unexpectedly changes a dependency, public API, persistence
schema, or security-sensitive entity can introduce new context and evidence
requirements. Dynamic resolution is a mandatory post-observation hook.

## Tool resolution

Structrr returns semantic requirements:

```yaml
tool_requirement:
  capability: capability:python-quality-validation
  reason: Python implementation sources changed.
  preferred_tool: tool:python-validation
```

Workrr resolves the tool's opaque external reference:

```yaml
tool_resolution:
  tool: tool:python-validation
  execution_ref: make:validate-python
  adapter: make-target
```

The adapter invokes the target. Make owns the command graph. CI owns its job
configuration. A scanner owns its policy file. Workrr captures result,
revision, relevant input fingerprints, and output reference as evidence.

Structrr decides whether that evidence satisfies an applicable product
requirement; Procedrr decides whether its procedure may transition.

## Proposal lifecycle

### Authoring

1. Workrr starts a compiled proposal procedure.
2. Structrr supplies the current product snapshot and context packets.
3. Procedrr collects bounded semantic decisions.
4. Workrr submits a candidate `SemanticPatch` to Structrr.
5. Structrr validates and materializes a proposal overlay.
6. The proposal stores compiler-derived read/write dependencies and context
   packet fingerprints.

### Pre-implementation rebase

1. Workrr binds the current design revision.
2. Structrr performs a three-way semantic rebase.
3. Clean and mechanical changes advance automatically.
4. A targeted report becomes input to a Procedrr amendment procedure.
5. Only invalidated decisions can be amended; unaffected operations remain
   sealed.
6. Structrr emits a new proposal revision.

### Implementation

1. Workrr binds design and source revisions to the execution.
2. Procedrr exposes one bounded implementation decision at a time.
3. Structrr resolves decision-time context and source bindings.
4. Workrr mediates edits and records observed source impact.
5. Newly applicable context and validation requirements are injected before
   subsequent decisions and completion.

### Merge reconciliation

1. Rebase the intended design patch onto current design.
2. Reconcile branch source against current source and source bindings.
3. Compare declared design impact with observed source impact.
4. Re-resolve current assurance and tool requirements.
5. Run or verify fresh external evidence.
6. Route substantive findings through targeted Procedrr decisions.
7. Ask Structrr for a revision-bound merge certificate.

## Rebase and targeted amendments

Structrr returns typed conflict items, each with affected patch operations,
changed dependencies, graph paths, and permitted amendment kinds. Workrr maps
those items into a sealed Procedrr worklist. The procedure cannot modify
unaffected proposal operations unless a new conflict is explicitly admitted.

This prevents a small upstream change from causing wholesale proposal
regeneration and unreviewed design drift.

## Source observation protocol

Workrr reports source facts rather than declaring product truth:

```yaml
source_observation:
  before: git:abc
  after: git:def
  changed_subjects:
    - python://src/query/api.py#QueryClient.search
  binding_changes:
    - binding:query-api-implementation
  observed_relationships:
    - source: entity:cli
      type: consumes
      target: entity:query-api
```

Structrr derives `SourceImpact` and compares it with the proposal. Accepted
binding migrations and design amendments are separate semantic patches with
their own provenance.

## Failure handling

Failures cross boundaries as stable diagnostics:

- Structrr: unresolved resource, incomplete coverage, stale patch, semantic
  conflict, broken binding, or alignment failure.
- Procedrr: invalid definition, unsafe decision, exhausted bound, invalid
  transition, or unsatisfied output contract.
- Workrr: provider failure, operation failure, stale runtime binding, evidence
  capture failure, or adapter resolution failure.

Workrr may retry transport failures within Procedrr limits. It MUST NOT repair
Structrr meaning or bypass an incomplete context result. Semantic failures are
routed to an explicit decision, suspension, or terminal result.

## Caching and invalidation

Context packets MAY be cached by the complete request and revision tuple.
Workrr invalidates a packet when:

- a referenced object version changes;
- a newly observed anchor could activate additional context;
- the design, proposal, source, or policy revision changes materially;
- source binding reconciliation changes an affected entity; or
- the selected lens definition changes.

Evidence is separately invalidated by changes to its declared inputs, external
tool reference, configuration fingerprint, or product requirement.

## Explainability

Workrr MUST persist which context packet informed each LLM decision. Structrr
MUST explain every included item and every alignment finding. Procedrr MUST
identify the node and contract under which the decision was made.

Together they answer:

- What did the agent know?
- Why was that context applicable?
- Which procedure permitted the decision?
- Which operation changed the world?
- What evidence supports completion?
- What later change made the decision stale?

## Integration invariants

1. Structrr product truth never depends on provider output alone.
2. Procedrr never embeds a mutable copy of Structrr context.
3. Workrr never decides product applicability by prompt construction.
4. Mandatory context resolution runs before every consequential decision.
5. Context is re-resolved after observed effects can change applicability.
6. External tools remain authoritative for executable implementation.
7. Every decision and certificate binds exact revisions and fingerprints.
8. Semantic conflicts produce targeted decisions rather than silent repair.
9. No package imports another package's private implementation to bypass the
   public contracts.
