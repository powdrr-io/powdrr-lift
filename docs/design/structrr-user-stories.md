# Structrr story contracts

The service represents customer behavior with four version 1 collections:
`user_stories`, `user_story_steps`, `user_story_transitions`, and
`user_story_cases`. They are optional in older documents. Omitting them declares
no story coverage. See the [implementation plan](../plans/structrr-user-stories-and-behavioral-testing.md)
for the normative fields and later interview, planning, and verification work.

The first implementation slice provides typed declarations, strict parsing,
structural graph validation, compiler allocation, digests, snapshot/proposal
projection, rebase comparison, bootstrap preservation, and context queries.
It does not derive interview readiness or certify a customer's behavior from a
story declaration, confirmation field, functionality binding, or passing command.

## Parsing and identity

`powdrr_service.structrr.user_stories.parse_story_catalog(snapshot)` returns
frozen dataclass records and indexes for stories, steps, transitions, and
observations. `validate_story_catalog(catalog, reference_catalog)` checks the
whole graph. Passing a pinned mapping catalog also resolves durable source IDs
and typed functionality/invariant/module references. Without it, validation is
local structural validation. Missing implementation bindings are reported as
unmapped steps rather than invented bindings.

Invalid fields raise `StoryContractError` with a stable `code` and `subject_id`.
Graph findings use the same identity/code convention in `StoryValidationReport`.
The validator permits loops but requires every step to be reachable from entry
and able to reach an outcome. Cases are finite connected entry-to-outcome walks;
repeated transition IDs retain their order. Case observations must occur on the
walk, and terminal observations cannot be omitted. Complete coverage and semantic
adequacy are separate later gates, not consequences of graph validity.

`allocate_story_edit` returns a JSON-serializable `AllocatedStoryEdit`. Persist
it in the edit checkpoint and pass prior `pending_edits` when allocating a batch.
The same checkpoint key replays the exact allocation; another request at that key
is rejected. New story IDs depend on checkpoint identity, not narrative text.
The caller supplies accepted history IDs so a removed whole-story ID cannot be
allocated again. Local counters are persisted on the story; removed step,
transition, case, and observation identities remain retired.

`apply_story_edits` checks the catalog fingerprint and each expected-before
fingerprint, projects the entire batch, then validates the complete graph and
identity evolution. Draft allocations may be incomplete; published candidates
must be complete. Behavioral compiler edits clear the previous confirmation;
they cannot grant replacement authority. Normal proposal revisions separately bind the full repository
snapshot fingerprint. `merge_story_catalogs` merges disjoint primitives and
rejects concurrent changes to one primitive or an invalid combined graph.

## Digests and confirmation

`story_contract_digest` includes the title, narrative, graph, observations,
invariants, and functionality references. It excludes confirmation, allocation
counters, transcript/source pointers, and derived cases. It hashes related-story
identities without recursively expanding their graphs.

`case_digest` binds a case and fixtures to its story contract digest. Set-like
collections are normalized; transition walks and fixture arrays preserve order.
A changed contract invalidates the previous confirmation digest. Parsing a
matching confirmation proves only that its digest matches; the interview adapter
must independently establish the recorded decision's authority and current
semantic review before calling a story ready.

## Existing operations and consumers

Story deltas use the existing add/change/remove operation envelope. A step's
behavioral `action` lives inside `content`, separate from the operation action:

```yaml
user_story_steps:
  - id: story:session-resume:step:1
    action: changed
    content:
      id: story:session-resume:step:1
      description: The maintainer requests resume for a persisted session.
```

New records use `added` with their complete typed record in `content`; removals
use `removed` with only the subject identity. Proposal compilation captures
expected-before fingerprints for amendments, and portable handoffs enforce them.
All operations in one transaction are applied before graph validation, allowing
atomic split/remove/rewire changes. Unchanged retired IDs and increasing counters
are checked against the previous state.

Bootstrap preserves canonical story declarations from specification sources and
projects tracked changelists in Git introduction order. Identical declared
additions are idempotent; conflicting declarations fail explicitly. Source
inspection does not infer or rewrite approved stories. The current-state report
and cached changelog index retain the same four collections. Specification
normalization preserves meaningful empty lists inside story records.

Context queries accept the four collection names. Desired-state handoffs and
multi-PR proposal serialization preserve the records and IDs. Rebase comparison
can bind a whole story or particular primitives through
`referenced_story_subject_ids`. Story changes appear in actual diff
`declaration_changes`, not observed implementation operations or behavioral
source-change evidence.

The fixture [session-resume.yaml](../../tests/fixtures/user_stories/session-resume.yaml)
contains a complete graph with normal and failure cases. Deterministic tests
exercise repository bootstrap, proposal handoff/reload, changelist/cache replay,
atomic graph changes, allocation replay, loops, and conflict boundaries. These
prove compiler and storage behavior; they do not measure model quality.
