# Objective

Implement the requested feature using the existing repository APIs.

# Requirements

1. Add @defer directive support so servers can send critical data first while deferring non-essential fields incrementally.
2. Add @stream directive support so servers can send critical data first while streaming non-essential fields incrementally.
3. Implement session.execute_incremental(query) as an async generator.
4. The async generator must yield result objects.
5. Each yielded result object must have .data, .has_next, .errors, and .extensions attributes.
6. The .data dict is accumulated across payloads, not raw deltas.
7. Each yielded result contains the .extensions from that specific payload, not accumulated across payloads.
8. Deferred fields merge into parent objects at the path given, using a data key in the incremental item.
9. For @stream, incremental items carry an items array.
10. For @stream, the path's last integer is the insertion start index into the parent list.
11. If an incremental item has no path field, treat it as root-level merge ([]).
12. Support nested paths navigating through lists by index.
13. Support nested paths navigating through null values.
14. Support nested paths navigating through field overwrites.
15. Support concurrent deferred/streamed fields.
16. Handle non-incremental responses gracefully.
17. Empty incremental arrays and hasNext-only payloads (without data or incremental fields) must still yield a result.
18. Errors must not halt subsequent items.
19. HTTP multipart transport with boundary=graphql and deferSpec=20220824 must support incremental delivery.
20. WebSocket transport must support incremental delivery.
21. The WebSocket transport must forward incremental payloads through the existing protocol.
22. Extend the DSL: .defer() on both DSLFragment and DSLFragmentSpread.
23. Extend the DSL: .stream() on list fields with optional label and initial_count parameters.

# Constraints

- Powdrr manages branching and commits.

# Relevant context

Inspected original files: gql/client.py, gql/dsl.py, gql/transport/aiohttp.py, gql/transport/websockets_protocol.py.

# Acceptance criteria

- Given An AsyncClientSession is connected to a transport that returns incremental payloads. The query uses @defer on a root field. The first payload has `data: {'user': {'name': 'Alice'}}` and `has_next: true`. The second payload has `incremental: [{'data': {'age': 30}}]` (no `path` field) and `has_next: false`. When The caller iterates over `session.execute_incremental(query)` and inspects the second yielded result's `.data` attribute. Then The second result's `.data` is `{'user': {'name': 'Alice'}, 'age': 30}`. The incremental item with no `path` field is treated as a root-level merge, so its `data` key's value (`{'age': 30}`) is merged directly into the accumulated root data dict, not nested under a `'data'` key. The first payload's `data` (`{'user': {'name': 'Alice'}}`) is preserved, and the second payload's incremental data is merged at the root, yielding the combined result.
- Given An AsyncClientSession is connected to a transport that returns incremental payloads. The query uses @defer on a field inside a list. The first payload has `data: {'users': [{'name': 'A'}, {'name': 'B'}]}` and `has_next: true`. The second payload has `incremental: [{'path': ['users', 1, 'age'], 'data': 30}]` and `has_next: false`. When The caller iterates over `session.execute_incremental(query)` and inspects the second yielded result's `.data` attribute. Then The second yielded result's `.data` is `{'users': [{'name': 'A'}, {'name': 'B', 'age': 30}]}`. The incremental item's path `['users', 1, 'age']` is navigated by first indexing into the `users` list at position 1 (the second element), then setting the `age` key to 30 on that element's dict. The accumulated data from the first payload is preserved, and the deferred field is merged into the correct list element.
- Given An AsyncClientSession is connected to a transport that returns incremental payloads. The query uses @stream on a list field `friends` at path `['user', 'friends']`. The first payload has `data: {'user': {'friends': [{'name': 'A'}, {'name': 'C'}]}}` and `has_next: true`. The second payload has `incremental: [{'path': ['user', 'friends', 1], 'items': [{'name': 'B'}]}]` and `has_next: false`. When The caller iterates over `session.execute_incremental(query)` and inspects the second yielded result's `.data` attribute. Then The second yielded result's `.data` is `{'user': {'friends': [{'name': 'A'}, {'name': 'B'}, {'name': 'C'}]}}`, with 'B' inserted at index 1 and 'C' shifted to index 2.
- Given A session has executed a query with a deferred field `user.friends` where the initial payload has `data: {user: {name: 'Alice', friends: null}}` and an incremental item `{path: ['user', 'friends'], data: [{id: '1'}]}`. When The session's `execute_incremental` async generator yields the incremental payload. Then In the yielded result's `.data`, `data['user']['friends']` is a list containing `{'id': '1'}`, replacing the initial `null` value. The null does not remain, and the list appears at the same path `['user', 'friends']`.
- Given A session has executed a query with two deferred fields: `user.friends` and `user.posts`. The initial payload has `data: {user: {name: 'Alice'}}`. The server sends two incremental items in the same payload: `{path: ['user', 'friends'], data: [{id: '1'}]}` and `{path: ['user', 'posts'], data: [{id: 'p1'}]}`. When The session's `execute_incremental` async generator yields that payload. Then The yielded result's `.data` is `{'user': {'name': 'Alice', 'friends': [{'id': '1'}], 'posts': [{'id': 'p1'}]}}`. Both `friends` and `posts` keys are present in `data['user']`; neither overwrites the other because each incremental item is merged at its distinct path into the accumulated data.
- Given A session has executed a query without any @defer or @stream directives. The transport returns a single non-incremental response with `data: {hello: 'world'}` and no `incremental` field. When The session's `execute_incremental` async generator yields that response. Then The yielded result has `.data` equal to `{'hello': 'world'}` and `.has_next` equal to `False`. The result is not treated as an error and is not skipped; it is yielded normally as the first and only result from the async generator.
- Given An AsyncClientSession is connected to a transport that returns a single non-incremental JSON payload: `{'data': {'hero': {'name': 'R2-D2'}}, 'hasNext': false}`. The session has `execute_incremental` implemented as an async generator. When The caller iterates over `session.execute_incremental(gql('{ hero { name } }'))` and collects the yielded results. Then The first (and only) yielded result object has `.data == {'hero': {'name': 'R2-D2'}}`, `.has_next == False`, `.errors is None`, and `.extensions == {}`. The iteration stops after that single yield because the payload's `hasNext` is `false` and no further payloads are expected.
- Given An AsyncClientSession is connected to a transport that returns a payload with `hasNext: true` but no `data` and no `incremental` field: `{'hasNext': true}`. The session has `execute_incremental` implemented as an async generator. When The caller iterates over `session.execute_incremental(gql('{ hero { name } }'))` and collects the yielded results. Then The first yielded result has `.data == {}`, `.has_next == True`, `.errors is None`, and `.extensions == {}`. The iteration does not terminate after this yield; it continues to wait for and process subsequent payloads from the transport, yielding further results as they arrive.
- Given An AsyncClientSession is connected to a transport that returns two payloads in sequence: first `{'data': {'hero': {'name': 'R2-D2'}}, 'hasNext': true}` and then `{'incremental': [{'path': ['hero'], 'data': {'friends': ['C-3PO']}, 'errors': [{'message': 'boom'}]}], 'hasNext': false}`. The session has `execute_incremental` implemented as an async generator. When The caller iterates over `session.execute_incremental(gql('{ hero { name friends } }'))` and collects the yielded results. Then The second yielded result has `.data == {'hero': {'name': 'R2-D2', 'friends': ['C-3PO']}}`, `.has_next == False`, `.errors == [{'message': 'boom'}]`, and `.extensions == {}`. The iteration continues and yields that second result despite the error in the incremental item.

Original requirements remain completion conditions when clarification is unresolved.

# Verification and completion

- Add or update relevant validations. Run applicable repository checks and report checks performed, failures and remaining ambiguity.
