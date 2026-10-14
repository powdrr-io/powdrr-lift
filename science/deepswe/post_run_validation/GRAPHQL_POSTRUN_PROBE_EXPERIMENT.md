# GraphQL post-run probe experiment

## Question

Can probes derived from the task instruction run after a coding-agent change,
independently of the coding agent, and produce reliable, actionable checks?

This experiment uses the `gql-incremental-graphql-delivery` task. The candidate
patch predates the probes, so the run is retrospective. It demonstrates
execution and diagnosis, not a blinded estimate of probe quality.

## Method

- Source instruction: `gql-incremental-graphql-delivery/instruction.md`.
- Probe catalog: `GRAPHQL_INSTRUCTION_PROBES.md`.
- Runner: `graphql_instruction_probe_tests.py`.
- Candidate: the retained coding-agent patch, applied to baseline commit
  `f07c89f` and tested in the task's Docker image with networking disabled.
- Control: the saved reference solution patch, applied to the same baseline and
  run in the same image.
- The runner sends JSON payload sequences through a local HTTP multipart server
  and the public `Client` / `session.execute_incremental` API. The transport
  server binds only to localhost. The WebSocket probe uses a local protocol
  server. DSL probes call the public DSL methods.
- Probes GQL-08 and GQL-09 remain unresolved and are not scored.

Input fingerprints for this final run:

| Input | SHA-256 |
| --- | --- |
| Task instruction | `0d14a5dac7fa2d6fe2e9d9d9a113fb02cfb2495f63f8b7edcaa18ba73dd16edf` |
| Probe catalog | `888eeafd4bf99ab110d77964a035e575582949a0be6c213d9ac4d3ba5c9b9cd1` |
| Probe runner | `b4f0365c2d2453292237abf7ebfefbea5802f99865a7e0500fe0d852ba410fc1` |
| Candidate patch | `f8b61cc5bbd0b18f14d240400b1181c94aff6c7b3645e2deb865d96b33d72c63` |
| Saved solution patch | `65d77639c640cde71e5e867089b4bfd55140e9e7d418183c3cb9df570ade30db` |

Both patch variants were applied to baseline `f07c89f`. The tests ran in image
`powdrr-science-gql-base:f07c89f8`.

The first harness version overrode `transport.execute_incremental` with raw
dictionaries. This bypassed processing in the saved solution's transport and
caused fixture incompatibility. The final runner instead uses the public HTTP
multipart transport for payload probes. The first reference run also showed
that a `hasNext`-only yield does not specify how a later `data` payload relates
to earlier data in the saved result contract, so GQL-14 checks only yielding
and continuation. The saved solution passes the final runner.

## Results

| Run | Result | Failures |
| --- | --- | --- |
| Saved reference solution control | 19 passed | None |
| Coding-agent candidate, final probe runner | 16 passed, 3 failed | GQL-05, GQL-10, GQL-15b |
| Candidate, task verifier | 13 passed, 5 failed out of 18 | Deep list-path merge, deferred-fragment errors, nested object update in a streamed list, parse-results compatibility, WebSocket incremental update |

The three probe failures line up with instruction-relevant verifier failures:

| Probe | Candidate observation | Related verifier failure |
| --- | --- | --- |
| GQL-05 nested list-index path | A patch at a list-index path left the existing member unchanged. | `TestDefer.test_deep_path_merge_with_list_elements`; the same merge defect also appears when patching `items[0].meta` in `TestStream.test_list_accumulation_with_nulls_and_nested_objects`. |
| GQL-10 concurrent defer and stream | The deferred update to `users[0]` was lost when combined with a stream update. | `TestWebSocket.test_incremental_over_websocket` loses a deferred update through the WebSocket path. |
| GQL-15b incremental item errors | The data from a later sibling entry was merged, but its preceding entry's error was absent from `.errors`. | `TestDefer.test_errors_in_deferred_fragments` reports zero errors instead of the two errors attached to incremental entries. |

The remaining verifier failure, `parse_results=True` compatibility, is
intentionally outside this instruction-derived set because it is an existing
client API behavior rather than a requirement in the task instruction.

## How this fits the pass-rate goal

The task verifier is the acceptance check: generated code needs to pass it.
These probes are a separate post-coding feedback step. Each failure can be
handed to the coding or repair agent as a concrete requirement, input sequence,
expected behavior, and observed behavior. For example: “GQL-15b failed: errors
on an incremental entry were omitted from `.errors`; preserve and surface
those errors while processing the later sibling entry.” The agent can repair
against that diagnosis before the task verifier is run again.

In this experiment, the probes flagged three defects that correspond to
instruction-relevant task-verifier failures. The saved solution passes all 19
probes, which is a useful false-positive check. This is the intended signal:
probes can identify concrete acceptance risks and make them actionable from the
instruction. We are not trying to replace or outperform the task verifier.

This run does not yet show that probe feedback raises pass rate: we did not send
the diagnostics to an agent and rerun the task verifier. Also, GQL-15b was
added after the verifier output exposed that error shape, so that particular
match is not an independent discovery. The next experiment should freeze the
probes before a fresh coding run, pass probe failures into a bounded repair
step, then run the task verifier before and after repair. Repeating this across
candidate runs will show whether this feedback makes agents pass the solution
tests more consistently.

## Reproduction

Run from the task repository in the pinned task image, mounting the candidate
checkout at `/candidate` and the probe runner at `/probes.py`:

```sh
docker run --rm --network none \
  -v /path/to/candidate:/candidate \
  -v /path/to/graphql_instruction_probe_tests.py:/probes.py:ro \
  -e PYTHONPATH=/candidate -w /candidate \
  powdrr-science-gql-base:f07c89f8 \
  python -m pytest -q /probes.py
```

The final source instruction, probe catalog, candidate patch, and runner should
be fingerprinted together when this is repeated as a prospective experiment.
