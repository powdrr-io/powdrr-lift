# Full-instruction context replay

## Result

The replay emitted **5 criteria for 5 of 23 routed requirements**, compared with
0 of 23 in `atomic-contract-v3`. All 115 calls completed with no provider
failures. The prompt keeps all 23 original requirements and adds criteria for
r004, r006, r015, r016, and r023.

This is a measured increase in criteria coverage, but not an unqualified quality
win. Manual comparison against the held-out solution and validation patches
found a materially invalid r015 criterion. Its `given` schema declares only
`id`, `title`, and `author` on `Book`, but its query uses an undeclared `reviews`
field. It also places the deferred item at path `["books", 0, "author"]` and
gives it data `{"author": ...}`. The validation patch's deferred items use the
parent object path (for example `["user"]`) and put the deferred fields in
`data`; for this criterion's analogous book object, the path should be
`["books", 0]`. The syntax-only query check and the model review both accepted
this invalid scenario. A coding agent following it could implement the wrong
merge behavior.

The other four criteria align with the task's requested behavior and the
solution/validation patches: yielding result objects (r004), accumulated data
across payloads (r006), a single result for a non-incremental response (r016),
and `.stream()` DSL arguments (r023). This is a manual audit of those five
criteria, not a full 23-requirement oracle score. The criteria also vary in
detail and still leave many routed requirements without a separate acceptance
criterion.

## What changed

The only experiment input change was directive evidence resolution: dependency
declarations for directives explicitly named in the full instruction are
available even when a routed child omits their names. The production splitter
context change is **not exercised by this replay** because its routed clauses
come from the frozen v3 input. This run therefore evaluates the upstream
evidence lookup, not the production split-context change.

All 115 current request hashes differ from the v3 hashes, so the provider
responses were regenerated rather than replayed from v3. The model and workflow
configuration stayed the same. As this is a one-shot model comparison, it
shows a promising outcome but does not establish that the evidence change alone
caused the increase.

| Run | Emitted criteria | Provider calls | Provider failures |
| --- | ---: | ---: | ---: |
| `atomic-contract-v3` | 0 / 23 | 115 | 0 |
| `atomic-contract-v4` | 5 / 23 | 115 | 0 |

`prompt.md`, `summary.json`, `provenance.json`, and `checkpoints.tar.gz` preserve
the complete generated prompt and run trace. The archive includes the baseline
v3 calls used for comparison as well as the 115 v4 requests. Solution and test
patches were inspected only after generation; they were not model inputs. No
coding agent or task tests ran.
