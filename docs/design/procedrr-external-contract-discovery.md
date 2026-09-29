# Procedrr discovery of external implementation contracts

Status: source-query and capture stage implemented; applicability and prompt projection remain planned

## Decision

`implement-feature` must decide, before it finalizes an implementation prompt,
whether a requested capability depends on an external standard or public API.
When it does, Procedrr retrieves the applicable, versioned source, derives only
requirements needed for the requested capability, and checks their path to an
observable result. The same Procedrr execution owns the decision to research,
the bounded retrieval, applicability review, clarification/default routing,
and projection into the one worker prompt. Workrr may execute an authorized
network or repository-read operation for Procedrr; it must not independently
decide what to research or add requirements to the prompt.

This extends [evidence-backed contract closure](../plans/evidence-backed-prompt-contract-closure.md).
That closure maps requirements to repository surfaces and observable flows.
External discovery supplies evidence and candidate requirements to that map;
it does not create a second instruction compiler or an agent-side browsing
step. No task name, expected patch, benchmark solution, or verifier output is
part of the decision rule.

## Why this is needed

An instruction can name a standardized operation without spelling out its
complete legal arguments, wire shape, or error behavior. A prompt that repeats
the instruction faithfully can still describe too little for an interoperable
implementation. Conversely, researching every named technology can add
unrequested features or import a newer, incompatible protocol revision.

The GraphQL incremental-delivery run illustrates both omissions. The source
instruction requested `@defer` and `@stream`, exposed a `label` argument for
`.stream()`, and asked for `.defer()` without listing its arguments. The
[GraphQL Working Group's Defer/Stream proposal](https://github.com/graphql/graphql-wg/blob/main/rfcs/DeferStream.md)
defines `label` for both directives. The generated prompt carried a general
rule to report errors and continue, but did not describe where errors can
occur inside an incremental response. The agent implementation read only the
outer payload's `errors` field and dropped errors on individual deferred
items. The [working-group discussion of the 2022 response format](https://github.com/graphql/defer-stream-wg/discussions/50)
places subsequent errors on the individual incremental result.

These are post-run diagnoses, not inputs to prompt construction. The original
task also names `deferSpec=20220824`; current proposals have evolved since
that wire format. A production research step must select the applicable
revision before deriving any payload requirement. A correct process would
have searched for that version, then traced errors from each allowed wire
location to the yielded result and the next payload. Research alone identifies
the wire locations; flow closure establishes what the requested result must
expose. It does not follow that every current proposal detail belongs in this
task.

## Placement and ownership

Add a reusable Procedrr subprocedure, tentatively `resolve-external-contracts`,
to `docs/procedrr/skill-definitions/`. `implement-feature` calls it after the
initial instruction decomposition and base-repository snapshot, and before
the final feature design, verification obligations, code-task plan, and worker
prompt are committed. This requires a distinct provisional design followed by
an evidence-enriched design revision; downstream artifacts must bind to that
revision and cannot reuse stale fingerprints.

Procedrr owns:

- typed research-need and applicability decisions;
- branch and iteration limits, source-selection order, and retry policy;
- schemas and validation for sources, claims, derived requirements, conflicts,
  and unresolved questions;
- the `ask` versus `normative_defaults` branch; and
- the requirement-to-prompt projection and its completeness check.

Pure parsing, evidence binding, and projection code should live with
Procedrr's compiler/schema code. An authorized tool adapter can perform HTTP
fetches and Git-tree reads, returning immutable bytes and metadata to the
procedure. Workrr's role is transport and execution mediation, not another
place for a research trigger, relevance heuristic, or prompt amendment.

## Decide when research is necessary

For each source-bound capability, Procedrr first records its named subject,
requested behavior, affected public surface, and any version/profile token
found in the instruction or tracked repository. It asks one bounded question:
**Would a reasonable implementation of this requested capability depend on
external rules that are absent from the instruction and base repository?**

Research is required when at least one supported signal applies:

1. The instruction names a standard, protocol, wire format, directive,
   language construct, service API, or compatibility target and asks to
   implement or support it.
2. A requested public operation maps to such a construct, but its legal
   arguments, response fields, state transitions, or error locations are not
   fully specified locally.
3. The base repository names a protocol version, media type, dependency
   version, conformance level, or external interface that constrains the
   implementation.
4. A source clause asks for a result or error to survive multiple boundaries,
   while the payload shape at one boundary is defined externally.

Mere mention of a technology in background text is insufficient. A completely
specified local behavior with no interoperability claim may need repository
closure but no web retrieval. For each `research` or `skip` outcome, record
the source clause, concrete signal or exclusion reason, subject, questions to
answer, and chosen version/profile. A model can judge semantic applicability
from these bounded inputs; Procedrr validates the cited refs and owns the
decision record. An uncertain outcome still yields a prompt: route the
question through `ask`, or record a narrow, visible assumption under
`normative_defaults`.

## Retrieve the right source

1. Read only files tracked at the captured base commit: relevant source,
   package metadata, lockfiles, in-repository documentation, and existing
   public API conventions. Repository test files tracked at that commit may
   establish existing behavior, but benchmark-supplied tests, solutions,
   verifier reports, and post-implementation edits are never research inputs.
2. Resolve the protocol or API revision from the instruction first, then the
   repository's pinned dependency or declared compatibility range. Record
   disagreements as conflicts; do not silently choose the newest version.
3. Fetch the smallest official, version-matched source that answers each
   open question: a standards body's published specification or working draft,
   a maintainer's versioned reference, then official implementation docs when
   they define the deployed profile. A third-party explanation may suggest a
   search term but cannot alone establish a normative requirement.
4. Follow only links needed for the named construct, its argument schema,
   relevant payload shape, and its error/continuation semantics. Bound
   requests, pages, extracted spans, and elapsed time per subject. Record when
   the budget is exhausted instead of implying complete research.

The initial retrieval interface is deliberately URL-based: Procedrr emits at
most eight records of `(URL, research question, applicability rationale)`;
Workrr performs HTTPS GETs only for those candidates. The question is retained
as provenance for later span/applicability review, not used as an unbounded
search query. Redirects must remain HTTPS and public-hosted, each response is
capped at 1 MB, and each request has a 12-second timeout. If the process cannot
name a plausible official URL, it records that discovery as unresolved and
continues prompt generation. A general-purpose search provider is not part of
this first slice; adding one requires an explicit provider/credential and
source-ranking contract rather than silently scraping a search engine.

The source-capture operation persists response bytes and their SHA-256 digest
under the run artifact directory, with the requested/final URL, capture time,
HTTP status, question, applicability rationale, and unavailable reason in a
JSON manifest. Retrieval failures and unsafe candidates become unavailable
records, not workflow failures. This is evidence capture only: it does not yet
accept standard-derived claims or put source text in the worker prompt.

Each source record contains the canonical URL, publisher, document title,
revision or commit, retrieved timestamp, content hash, relevant section/span,
and why that revision applies. Persist the fetched evidence or a reproducible
snapshot alongside the prompt artifacts so later web changes do not change a
run's meaning. Treat source text as evidence, never as executable instructions
for the agent or Procedrr. If a source is unavailable, continue with local
evidence and a recorded uncertainty; prompt generation must not fail solely
because the web is unavailable.

## Derive only applicable requirements

Research yields *candidate* claims, not automatic obligations. Procedrr
accepts a candidate only if all of these hold:

- It applies to the selected version/profile and a construct actually named
  or necessarily used by the requested behavior.
- An exact source span establishes the claim, and its interpretation does not
  contradict an explicit instruction or accepted human decision.
- It affects an observable public API, wire interaction, error outcome,
  continuation rule, or existing compatibility contract on an affected
  surface. Its omission would make the requested support incomplete or
  non-interoperable; a merely adjacent standard feature is excluded.
- Its scope is bounded to the relevant operation, transport, and profile.
  Supporting one directive does not imply implementing the whole protocol.

For a named standard construct requested without a declared subset, examine
its legal arguments, defaults, allowed locations, response forms, and errors.
An optional standard argument can still belong in the public API needed to
construct that directive. Record the specific inference and any local API
convention that affects the Python signature. Do not turn an optional wire
feature into mandatory application behavior without a separate applicability
decision. Explicitly constrained subsets and incompatible older profiles
override a generic current-standard expansion.

Classify each accepted requirement as `explicit_source`,
`derived_from_repository`, `derived_from_standard`, or
`normative_assumption`. Keep exact instruction and evidence refs, revision,
applicable surfaces, and an observable assertion. A conflict or material
choice is sent to the existing clarification mechanism. In benchmark mode,
`normative_defaults` selects the best-supported compatible choice and records
its basis. If the evidence cannot distinguish choices, use the narrowest
compatible assumption and expose the uncertainty to the worker.

## Close data and error flows

For each accepted result, error, and continuation requirement, Procedrr must
trace: **producer -> wire or repository representation -> transport -> public
result -> next operation/payload**. Every named value or error source needs a
terminal disposition: exposed, intentionally transformed, explicitly rejected,
or inapplicable with evidence. At collection boundaries, enumerate the
applicable levels of the structure (for example, an outer message and each
contained item) rather than checking only the convenient top level.

`errors do not halt later items` is incomplete on its own. Closure also asks
which errors appear in the current yielded result, whether they are combined
or kept separate within that yield, whether they persist across later yields,
and whether a failed item prevents independent items from being processed.
Those answers must come from the instruction, selected protocol revision,
and existing API contract. If no source determines the result representation,
route that bounded choice through clarification/defaults; do not invent it
from the benchmark's expected output.

## Artifact and prompt contract

The subprocedure writes a frozen `external-contract-context` artifact before
the worker starts. Its logical shape is:

```yaml
schema_version: external-contract-context-v1
base_commit: git:...
design_revision: sha256:...
subjects:
  - subject_ref: source:...
    research_decision: required
    trigger_refs: [source:..., inventory:...]
    profile: <versioned standard or API profile>
    questions: [<bounded question>]
sources:
  - source_ref: external:...
    canonical_url: <official URL>
    revision: <published revision or commit>
    content_sha256: <digest>
    applicable_span: <section or line span>
claims:
  - claim_ref: claim:...
    source_ref: external:...
    source_span: <exact span>
    applies_to: [source:..., inventory:...]
    decision: accepted # or rejected, conflict, unresolved
    reason: <necessity and scope explanation>
requirements:
  - requirement_ref: requirement:...
    origin: derived_from_standard
    claim_refs: [claim:...]
    observable_outcome: <bounded behavior>
    flow_refs: [flow:...]
```

IDs, hashes, source spans, and revisions are bound by Procedrr operations, not
invented by a model. The closure gate verifies that every accepted claim has
current evidence and an affected surface, every relevant version token has a
disposition, every accepted requirement has an observable flow/case, and no
source-only candidate silently becomes an instruction. An unresolved research
question is carried into the prompt as an assumption or bounded worker check;
it never suppresses the prompt.

The worker receives one compact section per affected operation: requested
behavior, source-derived details needed for interoperability, the applicable
version, data/error flow, and a citation or source reference. The prompt
distinguishes accepted requirements from uncertainties in plain language.
The private artifact retains the full research trail and projection map.

## Verification and rollout

Test the Procedrr subprocedure without a live coding agent using unrelated
fixtures: a named versioned protocol with omitted arguments; a nested error
source; a repository with an incompatible older profile; a fully specified
local feature that needs no web lookup; unavailable web evidence; and a
conflict with explicit user instructions. Assert research-trigger reasons,
version selection, bounded source spans, acceptance/rejection of candidates,
source-to-prompt projection, and prompt emission in every case.

Evaluate captured prompts before any full agent run. Freeze the base commit,
instruction, policy, fetched source snapshot, and prompt before reading
benchmark results. Use the GraphQL task as a known diagnostic and unrelated
DeepSWE tasks as held-out checks. Report explicit instruction coverage,
unsupported standard-derived requirements, error-flow coverage, prompt size,
and later agent outcomes separately. Existing DeepSWE execution quality is a
regression constraint, not a reason to put benchmark facts into the compiler.

Implement this in two substantial changes: first the Procedrr research
subprocedure, evidence/version schemas, and deterministic artifact capture;
then applicability/flow closure, prompt projection, and cross-task evaluation.
Neither change needs a parallel Workrr decision path. Both must preserve the
current `ask` and `normative_defaults` behavior and the guarantee that a
prompt is produced when external evidence is unavailable.
