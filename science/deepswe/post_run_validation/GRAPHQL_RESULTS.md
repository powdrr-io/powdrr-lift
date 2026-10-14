# GraphQL proof of concept: streamed-list overlap

## Frozen instruction and review question

The task instruction says:

> For `@stream`, incremental items carry an `items` array, and the path's last
> integer is the insertion start index into the parent list.

Before examining the saved solution or tests, turn that clause into this
question:

> With existing list `[a, b, c]` and incremental payload
> `path=["list", 1], items=[x, y]`, what happens to existing elements `b` and
> `c`? Are they replaced, shifted, or preserved at their positions?

This isolates an observable question for the post-run check. In this benchmark,
the saved reference solution is the correctness oracle, so its behavior
resolves the expected result: later elements at the addressed indices are
replaced, not shifted.

## Candidate and verification evidence

The candidate is the saved GraphQL task reference `solution/solution.patch` in
the DeepSWE task corpus. Its `_merge_stream_items` writes each item at
`start_index + offset`, extending with `None` only when the list is too short.
For the overlap case, that produces `[a, x, y]`: `b` and `c` are overwritten.

The added `tests/test_incremental_delivery.py` cases use stream starts at the
current list length or beyond it: empty-list index 0, `items` index 2 when its
initial length is 2, `posts` index 1 when length is 1, and similar append/pad
cases. The stream tests do not include a start index inside an already
populated list, so they cannot distinguish indexed replacement from shifting
insertion.

I ran the saved task's actual `tests/test_incremental_delivery.py` in an
isolated container using its saved `solution.patch` and `test.patch`. The
unmodified task suite passed: **18 passed**. I then added the overlap case
above as a temporary test in that module; the saved solution passed it, and
the module passed **19 tests**.

The focused assertion was:

```python
assert final.data["items"] == ["a", "x", "y"]
```

The initial response was `{"items": ["a", "b", "c"]}`; the incremental
payload was `{"path": ["items", 1], "items": ["x", "y"]}`.

To test whether the focused case added value, I made a temporary conditional
mutant: when a streamed index is inside the existing list, insert items and
preserve the later elements; when the index is at or beyond the list length,
keep the original append/pad behavior. Against this mutant, all **18 original
tests still passed**, while the new overlap case failed with
`[a, x, y, b, c]` instead of `[a, x, y]`. This is the discriminating result:
the question-derived check catches an incorrect behavior that the task's
existing validation suite accepts.

A broader mutant that inserted items at every index was caught by three
existing tests. That first result did not establish added value; narrowing the
mutant to the untested overlap condition produced the useful comparison.

The earlier lightweight replay is also available at
[`witness_graphql_stream_overlap.py`](witness_graphql_stream_overlap.py). It
mirrors the saved helper's list-write logic, but the task-suite experiment
above is the primary evidence.

```bash
rtk proxy /Users/gregory/code/powdrr-lift/.venv/bin/python \
  science/deepswe/post_run_validation/witness_graphql_stream_overlap.py
```

Observed overlap output: the saved solution matches the oracle result
`[a, x, y]`; the shift-preserving mutant yields `[a, x, y, b, c]` and fails the
focused check. The append control passes for the saved solution.

## Finding

The question identifies a real validation blind spot: the existing tests only
exercise append/pad behavior for `@stream`. The saved solution establishes the
oracle behavior for this task: addressed positions are replaced. The temporary
question-derived check both passes the oracle and rejects a mutant that passes
all 18 existing tests.

The post-run disposition, using the saved solution as the oracle, is:

- **Test coverage:** definite gap; no overlapping-index stream case checks this
  distinction. This is a validation finding, not by itself a task-compliance
  failure.
- **Candidate behavior:** correct; the saved solution replaces addressed
  positions, producing `[a, x, y]`. A criterion expecting
  `[a, x, y, b, c]` is incorrect against the oracle.

The focused check passes against the oracle behavior and fails against the
conditional shift-preserving mutant, so it discriminates the behavior the
existing suite leaves untested. This is a concrete value-add for post-run
validation: it turns an instruction clause into an executable check that
detects a plausible defect the current suite misses. It is still a one-case
demonstration, not evidence that the method generalizes across tasks.

## Live coding-agent run

The first actual Powdrr/Harbor run did **not** produce an agent candidate, so
it does not answer whether the agent can generate code that passes the saved
solution tests. Planning stopped before prompt generation: the phrase `null
values` in instruction clause 16 activated the `argument_presence` dimension
screen, and its classifier abstained. Contract compilation then failed with
`cannot compile a source contract with unresolved decisions:
argument_presence`. The report records no prompt, no changed paths, and no
validation command.

Pier's verifier consequently ran against the untouched baseline: **0/17
feature tests passed**, while **811/811 pre-existing tests passed**. The
feature failures all report that `AsyncClientSession` has no
`execute_incremental` method. This is a planner failure followed by a baseline
score, not an agent implementation result.

The artifact set is preserved at
`/private/tmp/pier-gql-agent-run-20261013-03/graphql-agent-validation-20261013-03/`.
I added a narrow regression test and fixed the dimension screen so ordinary
mentions of null list values do not request an argument-presence decision. The
next run must use that fix and a fresh Pier job root before we can evaluate
generated code against the saved solution tests.

## Limits and next step

This is a focused, manual review of one saved reference patch, not a coding-agent
run or a blinded comparison of reviewer prompts. The run used the actual saved
task test module in the cached `powdrr-science-gql-base:f07c89f8` container;
the focused test and mutants were temporary and were not committed to the task
repository. The next experiment should repeat the protocol on more instruction
clauses and blinded planted mutations, then compare holistic and per-obligation
reviewers on recall, false alarms, and review cost.
