# Planning confirmed customer stories

The source-ledger planner carries confirmed story contracts through requirements,
allocated work units, reviewed execution-plan YAML, multi-PR Structrr artifacts,
and generated execution workflows. A story reference includes its contract
digest, owned step/transition/case IDs, case digests, and direct dependency digests.
Existing plans without stories remain readable.

A planning source selects stories using canonical story sections, normal story
operation envelopes, or a `story_refs` array. Markdown sources can put these in a
complete YAML fenced block. Source references select complete stories. The service
resolves IDs against the pinned Structrr baseline and requires current confirmation;
code inference and a generated approval flag cannot establish authority. Conflicting
source declarations require an approved story proposal before planning.

The initial planner conservatively attaches the selected full graphs to the
source's non-setup requirements. Units inherit references from their assigned
requirements; items inherit their units' references. This preserves source IDs and
anchors and avoids asking a model to copy digests. It can allocate overlapping
ownership when a requirement spans runtimes. The typed contracts also support
smaller ownership slices whose union includes the full integration contract.
Question context contains the full selected graphs, cases and direct related-story,
invariant and functionality dependencies. Existing context budgets still apply;
exceeding them blocks the request rather than truncating branches.

If one item owns the complete story, that item carries required story validation.
When ownership spans items, the compiler adds a validation-only unit and PR item
after every implementation owner. Its dependencies, references and report path are
persisted in the normal plan and proposal. If implementation is already present,
the planner can allocate validation without inventing implementation work. Plan
compilation rejects lost references, conflicting revisions, incomplete full-story
validation, or a validation item that can run before an implementation owner.

Generated workflows carry exact references into the issue worker. Before coding,
the worker compares them with the reviewed proposal and current projected story
and dependency context. Concurrent story edits, removals, case revisions, and
related-story/invariant changes stop execution with a concrete impact report.
Unrelated additions do not invalidate a pin. The coding prompt includes the pinned
graph and observations. Single-feature execution uses the same confirmation gate
and writes `story-planning-context.json` alongside its existing design handoff.
The older generative planning path rejects story-aware inputs and directs them to
the source-ledger compiler; it cannot silently publish an unallocated story.

An impact report lists changed graph subjects and observations, affected stories,
case IDs, functionality links, and allocated plan items. Supplied receipts remain
unchanged historical records; stale status is attached to their impact entries.
The existing interview and normal Structrr proposal workflow own behavioral
choices. A `design_amendment_needed` repair request returns to that route. An
approved step split uses ordinary atomic add/remove/rewire operations, retires the
old identity, obtains current confirmation, and is replanned with fresh references.
Title repairs preserve story pins; changing their allocation requires a refreshed
approved proposal. Existing item input digests include the story/case pins.

These artifacts record planned coverage. Partial implementation or a passing PR
command cannot verify a whole story. Finite-case generation, assertion linkage,
execution receipts and current verification status are implemented in later slices
of the [user stories plan](../plans/completed/structrr-user-stories-and-behavioral-testing.md).

The deterministic integration test runs the native planning process with a scripted
provider, splits one story across Python and TypeScript implementation items, adds
integration validation, reloads all compiled artifacts, and exercises a confirmed
step split, stale-case rejection, dependency changes, repair preservation, and the
real issue-worker preflight. It proves the pipeline contract, not model quality or
behavioral execution of the target application.
