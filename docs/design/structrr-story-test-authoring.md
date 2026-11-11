# Story cases and the existing test author

Confirmed story cases enter the existing execute-PR test-author workflow. This
slice adds finite path discovery, pinned case obligations, assertion links and
required semantic review. It does not introduce another MiniSWE author or claim
that an authored test verifies a story.

## Finite cases

`expand_story_cases` traverses transitions in stable ID order. Its targets are
reachable transitions, outcomes and declared observations. Deterministic cycle-
closing edges also require zero, one and repeated traversals. Search uses a
configurable edge-expansion budget, defaulting to 1,000. Exhaustion returns
`case_expansion_incomplete`, useful candidate walks and every uncovered target.
A candidate is a suggestion, not a stored case or a new behavioral decision.

The existing story interview exposes `cases` after contract confirmation. The
interview refines candidate initial conditions and fixtures against the confirmed
graph and actual source answers, adds cases through ordinary compiler-owned edits,
and reviews them with the existing bounded semantic reviewer. Cases retain repeated
transitions, reference existing observations and use the original identity allocator.
Changing a derived case changes its case digest without changing the story contract.
Missing customer choices return to the user interview; the test author cannot choose
them. Graph coverage alone does not establish input-domain coverage.

The planner requires declared cases to cover every required graph target before
publishing a story validation item. Missing cases report candidate paths and route
back to a normal Structrr proposal. Implementation slices retain ordinary local AC
obligations and pinned story context. Full-case obligations belong to the item
allocated complete story validation, so each partial PR need not execute an entire
cross-PR story.

## Pinned test contracts

A test-level `story_refs` entry contains story ID and contract digest, case ID and
case digest, the connected ordered transition walk, resolved steps, and observation
IDs. Loop walks preserve repeated edges and steps. Required test cases, behavior
scenarios, feature instruction contracts, and verification obligations retain these
references. Cases compile deterministic `SC-` obligations alongside the existing
local `AC-01` aliases. Unknown or changed cases and stale fixture digests block use.

The existing MiniSWE author receives case fixtures, expected observations, the
confirmed graph, accepted structural operations and available entry-point evidence.
It still writes only the assigned test file in its isolated baseline checkout.
Non-story AC coverage retains its current behavior. The active author supports
pytest; a story validation request for an unsupported framework reports
`unsupported_story_test_author` and requires its supported author adapter.

## Assertion links

The test file supplies a module-level Python literal `__powdrr_story_links__`.
It maps real test function names to typed requests containing an obligation ID,
observation-to-assertion-line mappings, an assertion description, the public
integration boundary and declared external substitutions. The service validates
these requests and writes a versioned `story-test-links-v1` manifest. The model
does not supply authoritative selectors or source digests.

A local pytest plugin obtains the actual collected items. The service resolves
request names against those items, fingerprints the frozen test source and checks
assertion spans inside the linked test. Python assertions and recognized framework
assertions such as `pytest.raises` are supported. Comments, setup, nested helper
assertions, missing functions and uncollected selectors cannot establish a link.
Every required observation needs a link. One test may establish several cases;
several tests or files may establish one observation. Actual parametrized selectors
are retained rather than guessed from a function name.

The existing per-test quality reviewer receives pinned cases, linked assertions,
the authored module and entry-point evidence. For story obligations its findings
are a gate: mismatched fixtures, inadequate assertions, mocked targets, bypassed
public wiring, undeclared substitutions and unavailable review block acceptance.
Oversized review context blocks explicitly rather than truncating observations.
The compiler proves reference integrity and assertion syntax; semantic review
assesses adequacy. Neither proves runtime success by itself.

Corrections use the existing bounded MiniSWE correction path. They are recollected,
relinked, baseline-checked and reviewed before acceptance. Candidate collection must
match the reviewed source and selector manifest. Skips and expected failures do not
satisfy required story coverage. Authoring failures retain the generated sources,
review diagnostics and attempted link manifest. Successful runs retain a standalone
`story-test-links.json` and include it in the ordinary suite and execution reports.

## Remaining execution work

The existing baseline policy remains in force for this authoring slice. Integration
and existing-behavior baseline policies, candidate/environment-bound story receipts,
coverage status and the complete amendment-to-execution proof belong to the final
slice of the [user stories plan](../plans/structrr-user-stories-and-behavioral-testing.md).
No story is marked verified by the existence of these links or a green unrelated
command.

Deterministic tests exercise finite retry walks and budget exhaustion, ordinary
case allocation, the real MiniSWE provider boundary with scripted author output,
actual pytest collection and baseline execution, normal/failure/retry observations,
many-to-many links, stale cases, missing branches, unavailable collection/review,
comment-only assertions and mocked module wiring. These establish the pipeline
contract; they do not claim live model authoring quality.
