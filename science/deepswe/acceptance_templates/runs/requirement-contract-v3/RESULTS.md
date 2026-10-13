# GraphQL contract and acceptance comparison

**Result: no variant is ready to replace the previous prompt.** Extracting
contracts and adding model reviews increased output substantially, but did not
reliably remove semantic errors. Splitting construction into smaller decisions
reduced acceptance to five criteria without making those five trustworthy.

Same original instruction, 23 frozen routed requirements, base
`f07c89f8f065010a36b4263eded209b2b1d37063`, DeepSeek Flash and Typesafe.ai Jev.
No coding agents or tests ran. The solution and validation patches were read
only for manual evaluation, never passed to a model. This is an adaptive
single-task experiment, not a controlled causal or coding-success result.

## Captured variants

| Run | New provider calls | Cached decisions | Criteria in prompt | Prompt bytes |
| --- | ---: | ---: | ---: | ---: |
| Previous `symbol-grounded-v2` | 38 | 12 | 6 | 7,468 |
| `requirement-contract-v1` | 42 | 0 | 2, partial | 8,863 |
| `requirement-contract-v2` | 150 | 36 | 31 | 27,179 |
| `requirement-contract-v3` | 160 | 36 | 39 | 27,647 |
| `independent-contract-v1` | 135 | 0 | 27 | 20,317 |
| `atomic-contract-v1` | 115 | 0 | 5 | 14,679 |

The first contract run failed locally on Procedrr's collected-result wrapper
(`'rendered'`), after 42 completed calls. The collector was fixed and 36
unchanged extraction decisions reused. v2 completed; it was not successfully
interrupted. v3 added actual active-requirement scoping. There were no provider
failures in these five new runs. These are distinct adaptive experiments, not
retries attributed to transient provider errors.

v3's summary `covered: 23` means only that all 23 requirements had a
**model-approved proposal before consolidation**. It does not mean correct
coverage. Independent review initially accepted 25/42, attempted 17 repairs,
and finally accepted 27. Repairs often copied the erroneous draft. The final
atomic experiment used all 23 requirements and no previous generated outcome;
five passed its checks. Those five still fail the quality audit below.

## Concrete solution comparison

The previous prompt had an invalid defer example and HTTP `payload` wrappers.
The new dependency checks catch some invalid directive locations, and the
independent prompt contains a correct discriminating stream example:
`[a,b,c,d]`, path `[list,1]`, items `[x,y]` → `[a,x,y,d]`.
However, the same prompt also requires `[a,x,y,b,c]` for another stream update.
The correct solution writes addressed positions without shifting existing
elements. More criteria introduced a direct contradiction.

| Behavior | Evidence in the new experiments | Correct solution / remaining gap |
| --- | --- | --- |
| Result interface | Atomic `r004` mandates `ExecutionResult` and `hasNext` | Solution returns `IncrementalResult` with `.has_next`; the instruction specifies attributes, not concrete class identity |
| Accumulated data | Independent `r008-criterion-1` gives a correct object update preserving `name` | No emitted condition clearly checks that earlier yielded snapshots remain unchanged |
| Stream positions | Independent `r010-criterion-1` is correct; `r006-criterion-2` and `r019-criterion-2` shift existing elements | Addressed replacement, preservation of untargeted positions, and padding remain inconsistent or missing |
| Missing path | Independent `r011-criterion-1` gives a root data merge, but several others invent root stream append | Solution's missing-path stream item is not an append to an inferred field |
| Nulls | Atomic `r013` proposes traversal through a null parent and is withheld | Support for a null value at an addressed position is different from constructing missing parents through null |
| Overwrites | Independent `r014` places an object at a scalar terminal but expects the scalar inside it | Solution replaces the terminal with the supplied value; it does not unwrap the object |
| Progress-only messages | Independent `r005-criterion-2` adds `errors=[]` and `extensions={}`; atomic `r017` proposes `.data=None` | Solution yields retained accumulated data (or `{}` initially), current flags and `None` for omitted metadata in HTTP |
| Errors | Atomic `r018` only restates continuation and is withheld | Need concrete errored item followed by a successful item, with explicit observation of both data and error lifetime |
| HTTP / WebSocket | Independent `r019` invents output class/defaults; `r021` requires absent extensions to be `None` at the session layer | HTTP parts and WebSocket envelopes differ; WebSocket forwarding can place protocol controls in extensions |
| DSL parameters | Independent `r023` uses AST `initial_count` and requires zero AST arguments for `.stream()` | Solution encodes `initialCount` and emits default `initialCount:0`; equivalent default encodings should not be excluded |
| Existing API compatibility | Cited context includes batch APIs and WebSocket constructors | GraphQLRequest handling, serialization, result parsing, and generator closure need relevant declarations and observable conditions |

All solution comparisons are static. The validation patch contains 18 added
test functions, but none were run. A test name such as early-break cleanup is
not evidence that its assertions check the complete cleanup contract.

## Audit of all five atomic emitted criteria

Full proposals and reviews are preserved under `criteria/reviewed/` in the
atomic archive; `audit.json` lists these findings separately from model votes.

- **r004:** wrong concrete class and camel-case result attribute; payload
  values and required query variables are also unspecified.
- **r009:** repeats the server input as the expected result. An implementation
  that ignores the payload still satisfies this condition. It is not a client
  acceptance criterion.
- **r015:** invents payload values after setup. Its defer path ends in
  `description` while data is `{description: ...}`; the solution would put an
  object at that terminal, not produce the intended scalar field. No final
  accumulated output is specified.
- **r016:** validates legacy `execute`, not `execute_incremental` handling of
  a plain JSON response. It leaves the requested new API untested.
- **r022:** repeats method availability and combines DSL construction with
  execution; it does not specify which receiver produces which AST node.
  `DSLFragment.defer` operates on its spread, not an inline-fragment builder.

None is an unambiguous, useful, solution-consistent acceptance criterion.
This is a stricter finding than the previous audit's conditional-usability
labels; do not compare these as measured precision percentages.

## What the captured requests show

1. **Evidence selection is still too broad.** For atomic `r023`, the first
   three source excerpts are WebSocket constructors, followed by
   `DSLDirectable.directives` and `DSLDirective`. The shared batch combined
   WebSocket and DSL requirements; reranking its already-selected excerpts
   cannot recover the missing receivers. Dependency `ExecutionResult` and
   all directive definitions are supplied even to unrelated DSL questions.
2. **Existing source leaks into new obligations.** The generator and reviewer
   copy concrete return classes and unrelated wire conventions despite the
   question explicitly distinguishing existing evidence from new requirements.
   Installed dependency result declarations also describe a newer `id/subPath`
   protocol, while the instruction requests `deferSpec=20220824` with `path`.
3. **Decision splitting alone does not enforce dependencies.** Setup can be
   vague; the observation can invent values or assert the server's behavior.
   A later reviewer often approves it. There is no deterministic relationship
   between setup fields and asserted output values in these prose schemas.
4. **The renderer adds clutter.** Every approved compatibility fact is
   appended, including irrelevant batch APIs. Requirements, context and
   criteria overlap. The smallest new prompt is still nearly twice the
   previous prompt's size.

These observations localize the next change: retrieve declarations per active
requirement from the full symbol index, then supply only evidence relevant to
the requested receiver and operation. Record missing evidence explicitly.
Before another all-requirement run, inspect those deterministic packets.
Further reviewer wording or extra review passes have not earned promotion.
