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

## What this establishes

The final runner is an independent, repeatable post-run check: it passes the
saved reference control, fails the candidate on three concrete behaviors, and
maps those failures to requirements in the instruction. The probe diagnostics
are more directly tied to requirements than a raw verifier failure list and
can be turned into targeted repair feedback (for example: preserve list-index
deferred updates when processing a stream, and collect errors attached to
incremental entries).

This run does **not** establish new candidate coverage beyond the verifier: all
three candidate probe failures overlap instruction-related verifier failures.
It also does not show that a repair based on the probes improves the coding
agent's result, because no repair was run. The errors probe GQL-15b was added
after the verifier output exposed the missed error shape, so its agreement is
not an independent discovery. A next experiment should freeze the probe set
before a fresh coding run, then check both verifier overlap and new failures
across multiple candidate runs.

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
