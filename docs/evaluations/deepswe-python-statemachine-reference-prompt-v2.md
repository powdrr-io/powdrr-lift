# State data reference prompt, rebuilt from task evidence

Status: evidence-backed reconstruction. This prompt is grounded in the task
instruction, its 40 added test functions (72 verifier cases after sync/async
parameterization), and the reference solution patch. It describes tested
behavior explicitly and separates reference-solution choices and unresolved
requirements from the worker contract. It has not yet been used in a worker run.

## Evidence inspected

- Task: `python-statemachine-state-data-scoping`, base commit
  `8d17ba9f6ba8420cf05fddb94013bc221ed9a222`.
- User instruction: `tasks/python-statemachine-state-data-scoping/instruction.md`.
- Added tests: `tasks/python-statemachine-state-data-scoping/tests/test.patch`.
- Reference implementation: `tasks/python-statemachine-state-data-scoping/solution/solution.patch`.
- Verifier selection: `tasks/python-statemachine-state-data-scoping/tests/config.json`.

The test patch defines 40 functions. The verifier lists 72 new cases because
most async functions run in both sync and async modes. There is no test for
`DataVar()` with neither `default` nor `factory`.

## Rebuilt worker prompt

```text
Product contract:
Add state-owned data to python-statemachine. A state declaration may include
`data`, a dictionary whose keys must be strings. A missing `data` argument
means the state has no declared data; `data={}` is valid and means the state
has an explicitly empty data mapping. Invalid data containers and non-string
keys raise InvalidDefinition.

Keep active data on each state-machine instance, keyed by state. On state
entry, create a new outer dictionary from its declared defaults. Do not deep-
copy the values in that dictionary: ordinary non-callable defaults remain the
same objects across entries and machine instances. Use a callable or DataVar
factory when a fresh mutable value is required. On exit, remove that state's
active mapping after its on_exit callbacks have run; make the mapping
available to on_enter callbacks after initialization. Re-entry, including a
self-transition, starts from the declared defaults again. A state with no
data remains compatible with existing behavior. Do not store active values on
the shared State declaration.

For ordinary declared values, initialize the state's data key to that value.
For a callable used directly as a declared value, call it on every state entry
and use its result. `DataVar` supports a default, an optional type constraint,
and a factory. A factory is called on every entry; a callable DataVar default
is also called on entry. Reject a DataVar when both its default and factory
are non-None with InvalidDefinition. If both are non-None, reject it even when
the state is never entered. `DataVar(default=None, factory=fn)` is treated as
factory-only, because the implementation represents an omitted default and a
None default identically. `DataVar()` initializes to None. A None value is
accepted by a DataVar type constraint; validate non-None updates against that
constraint. These edge semantics match the reference solution; the added
tests only cover non-None default plus factory and a typed non-None update.
Export DataVar and DataChangeInfo from `statemachine`.

Callbacks receive `state_data` alongside their existing callback arguments.
It is a merged view: include declared data from active ancestors, then overlay
the current state's data so child values win on duplicate keys. Keep parallel
regions isolated. Mutations made through `set_state_data` during a callback
persist for the active state. Data must be initialized before on_enter and
remain available through on_exit.

Deep history restores saved data for the restored descendant configuration.
Shallow history restores data for the direct children it restores. Self-
transitions and repeated enter/exit cycles must follow the same reset and
cleanup rules as other transitions.

Implement these APIs:
- `get_state_data(state)` returns a copy of that active state's own data
  dictionary; it does not return the merged callback view. It returns None
  when the state is inactive or has no declared data, and `{}` for an active
  state explicitly declared with empty data.
- `state_data_values` returns a snapshot keyed by active state identifiers,
  containing each state's own data (not merged callback scopes) and excluding
  inactive states.
- `set_state_data(state, key, value)` updates an active state's declared key.
  Raise InvalidDefinition if the state has no active data, the key is
  undeclared, or a non-None value violates that key's DataVar type constraint.
- `get_data_changes()` returns DataChangeInfo records for updates, with
  `state_id`, `key`, `old_value`, and `new_value`. Keep records for the current
  macrostep and clear them at its boundary.

Preserve active state data across pickle round-trips. Allow data declarations
on compound and parallel states. Parse SCXML datamodel/data `id` and `expr`
values as Python literals. Annotate declared data variable names in generated
diagrams using the existing diagram conventions. Preserve machine behavior
when data is not declared.

Required product changes:
Implement the public behavior above through the existing state, callback,
history, SCXML, serialization, and diagram paths. Choose internal structures
from repository conventions; no particular storage class or helper layout is
required.

Validation contract:
Add or preserve focused checks for each behavior below. Run the new cases in
both synchronous and asynchronous modes where the runner supports both, plus
relevant existing tests for changed integrations.

1. State data declaration accepts valid dictionaries, multiple values, and
   ordinary values including None, bool, list, and dict. Empty dictionaries
   are valid. Non-dictionaries and non-string keys raise InvalidDefinition.
2. Active data is available through `get_state_data`; absent/inactive states
   return None; data-free states remain compatible. `state_data_values`
   contains own data for active state IDs and excludes inactive states.
3. Callback `state_data` includes state data, preserves existing callback
   arguments, combines ancestor and child values, gives child values precedence
   on collisions, and keeps parallel regions isolated. Verify parent-only and
   child-only keys together. Verify data exists before on_enter and through
   on_exit, and callback updates persist while active.
4. Exit removes data. Re-entry, self-transition, and multiple entry/exit
   cycles restore defaults. A transition from a data state to a state without
   data leaves neither state's inactive data accessible.
5. Plain callable defaults and DataVar factories are called again on re-entry
   and produce distinct mutable values. A typed DataVar default initializes
   to its declared value; an invalid runtime update raises InvalidDefinition.
   DataVar with both default and factory raises InvalidDefinition.
6. Deep history restores data for descendants; shallow history restores data
   for direct children. Check restored data from callbacks after history entry.
7. `set_state_data` accepts a valid update and rejects inactive states,
   undeclared keys, and values of the wrong DataVar type with
   InvalidDefinition. Change records expose the specified old/new values and
   fields, and are cleared at macrostep boundaries.
8. Pickle round-trip preserves active values. Compound and parallel state
   declarations work. SCXML datamodel declarations populate state data and
   behave like declarations made through the Python API.
9. Generated diagrams annotate declared data variable names. Preserve the
   existing no-data diagram behavior.

Keep assertions for all listed outcomes. Do not add requirements for
unspecified error handling or fallback behavior without evidence from the
instruction or established repository conventions.
```

## Coverage trace: added tests to prompt clauses

| Added test functions | Prompt clauses |
| --- | --- |
| `test_state_with_data_initializes_on_entry`; `test_state_data_accessible_via_callback_parameter`; `test_state_data_modified_in_callback_persists_within_state`; `test_multiple_data_variables_on_one_state`; `test_data_with_different_types` | Product contract declaration, lifecycle, callback and validation items 1–3 |
| `test_child_inherits_parent_compound_data`; `test_child_data_shadows_parent_data`; `test_callback_in_child_sees_merged_data`; `test_parallel_regions_have_isolated_data` | Product contract callback scope and validation item 3 |
| `test_data_initialized_before_on_enter`; `test_data_accessible_during_on_exit`; `test_data_cleaned_up_after_exit`; `test_reenter_state_reinitializes_data` | Lifecycle and validation item 4 |
| `test_deep_history_restores_data`; `test_shallow_history_restores_data` | History and validation item 6 |
| `test_get_state_data_returns_dict_for_active`; `test_get_state_data_returns_none_for_inactive`; `test_state_data_values_returns_snapshot` | API contract and validation item 2 |
| `test_non_dict_data_raises_invalid_definition`; `test_non_string_keys_raise_invalid_definition`; `test_empty_dict_data_is_valid`; `test_state_without_data_backward_compat` | Declaration contract and validation items 1–2 |
| `test_pickle_round_trip_preserves_state_data` | Persistence contract and validation item 8 |
| `test_scxml_datamodel_parsed_and_applied_to_state`; `test_scxml_state_data_works_like_python_api` | SCXML contract and validation item 8 |
| `test_compound_state_with_data`; `test_parallel_state_with_data` | State-kind contract and validation item 8 |
| `test_transition_from_data_state_to_no_data_state`; `test_self_transition_reinitializes_data`; `test_multiple_entry_exit_cycles` | Lifecycle and validation item 4 |
| `test_datavar_with_default_and_type`; `test_datavar_factory_creates_fresh_list_on_each_entry`; `test_datavar_type_validation_on_set_state_data`; `test_datavar_default_and_factory_raises_invalid_definition` | DataVar contract and validation item 5 |
| `test_set_state_data_updates_value`; `test_set_state_data_on_inactive_state_raises`; `test_set_state_data_with_undeclared_key_raises` | Mutation API and validation item 7 |
| `test_get_data_changes_returns_changes_after_set`; `test_changes_cleared_between_macrosteps` | Change records and validation item 7 |
| `test_callable_default_creates_fresh_instance_on_each_entry` | Callable defaults and validation item 5 |

The table is deliberately by test function: sync/async variants exercise the
same assertions and are not separate product requirements.

## Evidence limits and decisions still needed

1. **Mutable literal defaults:** The prompt explicitly matches the reference
   solution: the outer mapping is recreated, while non-callable values are
   retained by reference. The instruction's phrase “fresh copy of defaults”
   could imply deeper copying, but neither the added tests nor the solution
   support that interpretation. This is now an explicit contract choice, not
   an assumption left for the worker. Add a test if this choice is intended
   to be enforced.
2. **`DataVar` presence semantics:** The prompt explicitly matches the
   reference solution's value-based check: both values must be non-None to
   count as “both supplied.” `DataVar(default=None, factory=list)` therefore
   uses the factory. No test covers this case. If the public contract intends
   argument-presence semantics instead, the API needs a sentinel default and
   a test for explicit None plus factory.
3. **`DataVar()` behavior:** The reference solution initializes it to None;
   no added test requires this. The prompt now states this choice explicitly.
4. **DataChangeInfo import:** The instruction and solution export it, but the
   added test patch does not directly assert importing `DataChangeInfo` from
   `statemachine`.
5. **Diagram verification:** The instruction and solution require/support
   annotations, but the added test patch has no diagram test. The prompt asks
   for one because otherwise this explicit product requirement is unverified.
6. **Other untested edges:** The tests do not verify `state_data_values` copy
   isolation, query behavior for an active state declared with `{}` (the
   constructor validity is tested separately), exact preservation of other
   callback arguments, per-instance isolation of mutable literal defaults,
   SCXML parse errors, or type enforcement for initial DataVar values. The
   prompt follows the solution where available and explicitly identifies the
   untested decisions.

## Conclusion

This version is longer than the earlier draft because the task has interacting
scope, lifecycle, history, and API rules. It is explicit about all tested
behavior, the sync/async test structure, and the solution's choices for
untested edge cases. The mutable-default and explicit-None rules are source to
solution discrepancies worth reviewing, but they no longer leave the worker
to guess. The prompt has not yet been tested in a worker run; this trace alone
does not prove it will produce a patch that passes all 72 verifier cases.
