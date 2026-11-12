# Interactive Structrr story design

`specify-a-feature` keeps the existing prose interview, module scope, quality,
delivery-order and PR preparation flow. Its design step now calls the private
`story-design-interview` Procedrr process. This process uses service-owned typed
story edits and bounded semantic reviews, then displays the complete graph for
actual user confirmation. The headless `design-interview` semantic compiler
continues to compile obligations; it does not grant user-confirmation authority.

This implements the second slice of the
[user stories plan](../plans/completed/structrr-user-stories-and-behavioral-testing.md).
Story allocation through implementation plans, behavioral test authoring and
execution receipts remain later slices.

## Service contract and checkpoints

`StoryInterview.start` captures the actual original request with the existing
instruction ledger. A draft checkpoint contains the accepted baseline, compiler
allocations, graph, durable source records, questions and answers, semantic
reviews, and the pending display. New stories use compiler-allocated IDs;
`story_mode: amend` with `existing_story_ref` selects an accepted story instead.
One interview owns one customer goal. A later chat execution can add another
story or amend an existing one without requiring product-code changes.

The local chat adapter exposes one `story_interview` operation with a bounded
action catalog: start, select, context, edit, ask, review, present, status and
publish. It does not expose an operation that accepts an answer, a confirmation
flag, or a model-authored review. The typed `story_options` object carries
`story_mode: create|amend` and an optional existing story reference.

The adapter saves checkpoints and actual earlier interview answers under the
configured repository's `.powdrr/procedrr-runs/` directory. Resume verifies
checkpoint and instruction-ledger fingerprints and reuses prior allocations.
Repeated edits with unchanged keys/inputs do not allocate again. Accepted
questions return their prior answers rather than asking again. These private
drafts are not canonical repository design.

Draft edits use the first slice's normal allocator and projection rules. The
preview path permits an incomplete graph while adding nodes and edges. Readiness
and publication require whole-graph validation. The service clears confirmation
when behavior changes; source refs are selections from actual ledger/answer or
accepted-source catalogs, not newly invented model IDs.

## Review and readiness

A review concerns one story and one policy dimension. The decision route returns
only resolved, ambiguous, contradictory or not_applicable. A separate planning
call supplies the explanation and, for unresolved findings, one focused question.
Each result binds a typed decision specification, story contract digest, related
story/invariant/functionality digests, source evidence and reviewer-policy version.

All thirteen policy dimensions must be addressed. Source fidelity, step atomicity
and observability always apply. Non-applicability requires a rationale. Invalid
schemas and provider failures remain unavailable reviews, with retained failure
history; operational retries are bounded to three attempts per unchanged input.
Ambiguous semantic answers are not repeatedly resampled until a preferred answer
appears. Oversized review context produces an explicit unavailable result and a
partitioning instruction, without truncating the story.

Context retrieval is scoped by keywords/modules and returns `complete: false`
when its 64-record budget omits matches. Reviews include the selected story and
its direct related stories; they do not recursively expand cycles or include
unrelated graphs. Before presentation and publication the adapter checks current
repository dependency digests. A changed or removed referenced subject blocks
publication and requires refreshing the interview against current context.

Readiness is derived as draft, needs_confirmation, or ready. A valid declaration
and a matching `confirmation` field alone are insufficient. A ready story needs
current successful reviews and a recorded user response to the exact displayed
contract and context. Benchmark execution and normative-default policies cannot
acquire `user_confirmation` authority.

## Actual user confirmation

The service renders actor, goal, scope, preconditions, entry, technical step
descriptions, conditions/destinations, terminal outcomes, observations, state
effects, related stories, invariants and proposed-assumption responses. The
confirmation prompt names the exact contract digest. The user can reply confirm
or describe changes. A rejected assumption remains a recorded user response;
it is never marked accepted merely because the assistant proposed it.

Only the chat adapter calls `accept_user_response`, after receiving real input.
Deferred answers must match the caller-supplied resume value and the persisted
question text, and can be consumed once. A model cannot manufacture this authority
by supplying `__procedrr_resume__`, reusing another question's answer, or returning
`confirmed: true`. The caller-owned checkpoint remains the trust boundary; its
content hash detects corruption, rather than acting as an approval signature.

The interpreter retains full resumed operation outputs, including source refs,
when another question later suspends the process. This prevents multi-question
resume from replacing an earlier accepted answer with its original placeholder.

## Proposal and publication

After the authoring fragment finishes, a deterministic service gate independently
requires readiness and compiles the existing `ProposalRevision` envelope. It
writes these artifacts under `docs/proposals/<work-item-name>/`:

- `story-design.yaml`: normal story section operations.
- `story-proposal-revision.json`: the baseline-bound Structrr proposal.
- `story-interview-evidence.json`: scoped source, review and confirmation evidence.
- `design-interview-input.json`: a seed for existing feature-proposal generation.

The feature workflow completes and evaluates its other specification sections
through existing tools. It preserves the compiled story sections, includes all
four artifacts in publication, and retains the story operations in the PR
changelog. Bootstrap applies reviewed story-source deltas in Git introduction
order and deduplicates identical operations also recorded in changelists.

At the Git/GitHub publication boundary, the adapter checks private readiness,
current dependencies, the exact proposal and evidence, the seed, canonical story
declaration and generated feature story sections. An explicit publication file
list must contain every required story artifact; direct head publication must
contain their exact committed contents. No product implementation or behavioral
verification is inferred from publishing these declarations.

The deterministic native-process test begins with prose, proposes and records a
failure-path assumption, pauses twice across fresh adapters, preserves the same
IDs and accepted source refs, confirms the displayed graph, publishes a normal
proposal, and reloads it through real repository bootstrap. Paired cases reject
stale confirmation, model-authored approval, empty authoring, failed reviews,
changed dependencies and altered publication artifacts. These tests establish the
pipeline contract; they do not claim measured live-model interview quality.
