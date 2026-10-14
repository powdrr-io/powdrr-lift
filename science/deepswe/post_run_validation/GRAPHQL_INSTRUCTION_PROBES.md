# GraphQL instruction-derived probe set

This probe set is derived from the task instruction only. It is a frozen
candidate for binding to the task repository's test harness; no saved solution
or verifier test was used to select expected results. Source references below
are line numbers in `instruction.md`.

## Probe format and oracle labels

- **Explicit**: the instruction says what the result must be.
- **Contract-dependent**: the instruction requires a capability, but the exact
  result or representation needs the repository's existing API contract.
- **Unresolved**: more than one behavior fits the instruction; do not score it
  as a candidate failure until the contract is clarified.

Each probe changes one behavior at a time where practical. The harness still
needs to bind the payload operation to the task repository's public API and
transport fixtures. Those bindings should use the unmodified task baseline;
they must not inspect the saved solution.

## Core result and merge probes

| ID | Source | Concrete setup and action | Expected assertion | Oracle | Discriminating mistake |
| --- | --- | --- | --- | --- | --- |
| GQL-01 accumulated data | Instruction line 3: `.data` is accumulated, not raw deltas | Yield `{"data":{"user":{"id":"u1","name":"old"}},"hasNext":true}` then `{"incremental":[{"path":["user"],"data":{"name":"new"}}],"hasNext":false}`. Consume both results. | The second result has `data == {"user":{"id":"u1","name":"new"}}`; it is not just `{"name":"new"}`. | Explicit | Returning only the most recent delta. |
| GQL-02 payload-local extensions | Line 3: extensions belong to that payload, not an accumulated map | Yield payloads with `extensions={"trace":"first"}` and then `extensions={"cost":2}`. | First result has the first mapping; second has exactly the second mapping and does not retain `trace`. | Explicit | Accumulating or reusing extension values across results. |
| GQL-03 deferred object merge and overwrite | Line 3: deferred `data` merges at the supplied path; fields can be overwritten | Initial data: `{"user":{"id":"u1","name":"old"}}`. Apply incremental item `{"path":["user"],"data":{"name":"new"}}`. | Final user is `{"id":"u1","name":"new"}`: the addressed field changes and its untouched sibling remains. | Explicit | Replacing the entire parent object, or refusing to overwrite `name`. |
| GQL-04 root-level merge when path is absent | Line 3: missing path means root-level merge (`[]`) | Initial data `{"left":1}`; apply an incremental item with no `path` and `data={"right":2}`. | Final data is `{"left":1,"right":2}`. | Explicit | Treating absent path as invalid or as a nested path. |
| GQL-05 nested list-index path | Line 3: nested paths navigate through lists by index | Initial data `{"org":{"teams":[{"members":[{"id":"m1","name":"old"}]}]}}`. Apply `{"path":["org","teams",0,"members",0],"data":{"name":"new","active":true}}`. | The addressed member has `id="m1"`, `name="new"`, `active=true`; surrounding objects and list positions remain intact. | Explicit | Treating integer path components as object keys, or updating the wrong list entry. |
| GQL-06 null update | Line 3: null values are supported | Initial data `{"user":{"nickname":"Ada"}}`; patch path `user` with `{"nickname":null}`. | Final `nickname` is present and null, rather than ignored or replaced by a missing value. | Explicit | Dropping null as if it meant “no update.” |
| GQL-07 stream append at the current end | Line 3: stream `items` use the last path integer as their start index | Initial data `{"items":["a"]}`; apply `{"path":["items",1],"items":["b","c"]}`. | Final items are `["a","b","c"]`. | Explicit for this append case | Ignoring the path index, appending only one item, or confusing `items` with deferred `data`. |
| GQL-08 stream overlap semantics | Line 3: the path integer is called the “insertion start index” | Initial items `["a","b","c"]`; apply path `items,1` with items `["x","y"]`. | Ask whether the result should replace addressed entries (`["a","x","y"]`) or insert and shift them (`["a","x","y","b","c"]`). | **Unresolved**: “insertion start index” does not explicitly define overlap behavior. | This probe distinguishes overwrite from shift-preserving insertion, but must not be scored until the API contract resolves the wording. |
| GQL-09 stream beyond current length | Line 3: null values and indexed stream updates are supported | Initial items `["a"]`; apply path `items,3` with items `["d"]`. | Record the resulting list and whether gaps are padded, rejected, or treated another way. | **Unresolved**: the instruction gives no out-of-range policy. | Distinguishes padding, append, and failure behavior. Do not assert one without a contract. |
| GQL-10 concurrent defer and stream | Line 3: deferred and streamed fields can be concurrent | Initial data `{"users":[{"id":"u1","name":null}],"count":0}`. Stream payload: `{"path":["users",1],"items":[{"id":"u2","name":null}]}`. Deferred payload: `{"path":["users",0],"data":{"name":"Ada"}}`. Run both payload orders. | Final data contains user 1 as `{"id":"u1","name":"Ada"}`, user 2 as `{"id":"u2","name":null}`, and `count:0`. | Explicit for support and preservation; ordering equivalence is a probe condition to check for unintended order dependence. | One update overwrites or loses the other based on arrival order. |

## Yielding, completion, and errors

| ID | Source | Concrete setup and action | Expected assertion | Oracle | Discriminating mistake |
| --- | --- | --- | --- | --- | --- |
| GQL-11 result object contract | Line 3: each yield has `data`, `has_next`, `errors`, and `extensions` | Feed an initial payload and one incremental payload; inspect every yielded object. | Every yielded object exposes all four attributes. Compare each value to the applicable payload/result contract without assuming defaults for absent fields. | Explicit for attribute presence; absent-field defaults are contract-dependent. | Returning raw dictionaries or omitting fields on some yield paths. |
| GQL-12 non-incremental response | Line 3: non-incremental responses are handled gracefully | Feed only `{"data":{"ok":true}}` through `execute_incremental`. | Iteration completes without error and yields a result whose data contains `{"ok":true}`. | Explicit for graceful handling; exact completion metadata is contract-dependent. | Assuming every response must contain an incremental envelope. |
| GQL-13 empty incremental array still yields | Line 3: empty `incremental` arrays still yield | Feed `{"data":{"ok":true},"incremental":[],"hasNext":true}`. | One result is yielded for this payload even though the incremental array is empty. | Explicit | Silently skipping the payload because it contains no patches. |
| GQL-14 hasNext-only payload still yields | Line 3: `hasNext`-only payloads still yield | Feed `{"hasNext":true}` between two ordinary payloads. | The hasNext-only payload produces a yield and does not prevent consuming the following payload. | Explicit for yield/continuation; defaults for absent `data`, `errors`, and `extensions` are contract-dependent. | Dropping control-only payloads or terminating early. |
| GQL-15 errors do not stop later items | Line 3: errors must not halt subsequent items | Feed an initial result, then an incremental item containing an `errors` array, then a valid recovery patch. | The recovery patch is yielded and reflected in accumulated data after the error payload. Inspect `.errors` on the relevant result, but defer its exact aggregation/default assertion to the result-object contract. | Explicit for continuation; exact error placement is contract-dependent. | Raising or terminating on the first incremental error. |

## Transport and DSL probes

| ID | Source | Concrete setup and action | Expected assertion | Oracle | Discriminating mistake |
| --- | --- | --- | --- | --- | --- |
| GQL-16 HTTP multipart | Line 5: HTTP multipart with `boundary=graphql, deferSpec=20220824` | Send an HTTP multipart response with that media type: initial data part followed by a deferred part. Run it through `session.execute_incremental`. | The client yields the same accumulated results as GQL-01/GQL-03. | Explicit for transport support; exact MIME framing should follow the task's named media type and baseline parser contract. | Parsing only ordinary JSON or mishandling the named multipart parameters. |
| GQL-17 WebSocket forwarding and parity | Line 5: WebSocket supports incremental delivery and forwards payloads through the existing protocol | Deliver the same initial/deferred payload sequence as GQL-16 inside the existing WebSocket protocol's incremental message. | The incremental payload reaches the same result accumulator; yielded data matches the HTTP semantic sequence. | Explicit for forwarding/support; harness binds the existing protocol envelope from the unmodified repository. | Treating the WebSocket message as a terminal result or dropping its incremental payload. |
| GQL-18 `.defer()` on `DSLFragment` | Line 7 | Construct a fragment, call `.defer()`, and render/use it in a query. | The method exists and the generated query represents that fragment as deferred. | Explicit for API/mode; exact rendering follows existing DSL conventions. | Method missing or directive omitted. |
| GQL-19 `.defer()` on `DSLFragmentSpread` | Line 7 | Construct a fragment spread, call `.defer()`, and render/use it in a query. | The method exists and the generated query represents that spread as deferred. | Explicit for API/mode; exact rendering follows existing DSL conventions. | Supporting fragments but not spreads. |
| GQL-20 `.stream()` option matrix | Line 7: list fields support optional `label` and `initial_count` | Build/render four list-field cases: no options, label only, initial count only, and both. | `.stream()` is callable with each supported option combination, and specified options appear on the generated `@stream` directive. | Explicit for options/API; exact GraphQL argument spelling and defaults follow repository conventions. | Requiring both options, dropping one option, or allowing use on non-list fields. |

## How to use this set

Before running against any candidate, bind each row to a test harness and
resolve contract-dependent details from the unmodified repository. Keep
GQL-08 and GQL-09 marked unresolved unless a source other than the saved
solution settles them. Then freeze this file (or its machine-readable
equivalent) with a fingerprint.

After a coding run, execute the frozen probes against the candidate. For every
failure, retain the probe ID, assertion, and observed value and pass that
diagnostic into the existing bounded repair loop. Finally compare the probe
results with the task verifier: an instruction-derived probe adds coverage
only if it catches a behavior the verifier suite does not already distinguish.

Repository compatibility checks such as whether `parse_results=True` also
applies to `execute_incremental` are intentionally excluded: they come from
existing client API behavior, not this task instruction.
