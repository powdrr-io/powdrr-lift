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

- Original code paths: gql/client.py, gql/transport/websockets_protocol.py.

# Acceptance criteria

- Given A GraphQL client session is connected to a server that supports incremental delivery. The server sends a multipart HTTP response with boundary=graphql and deferSpec=20220824. The first payload is: {data: {user: {id: 1}}, hasNext: true}. The second payload is: {hasNext: false} (no data, no incremental field). When The client calls session.execute_incremental(query) and iterates over the async generator. Then The second yielded result object will have .data equal to the accumulated {user: {id: 1}}, .has_next equal to False, .errors equal to None, and .extensions equal to None (since the second payload contains no extensions field). This is because the .data attribute accumulates across payloads, while .extensions reflects only the current payload's extensions (which is None for the second payload). The generator yields a result even though the second payload has no data or incremental fields, as it's

Original requirements remain completion conditions when clarification is unresolved.

# Verification and completion

- Add or update relevant validations. Run applicable repository checks and report checks performed, failures and remaining ambiguity.
