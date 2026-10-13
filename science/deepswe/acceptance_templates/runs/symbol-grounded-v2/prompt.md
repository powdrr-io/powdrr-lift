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

Inspected original files: gql/client.py, gql/dsl.py, gql/graphql_request.py, gql/transport/aiohttp.py, gql/transport/aiohttp_websockets.py, gql/transport/async_transport.py, gql/transport/common/adapters/aiohttp.py, gql/transport/common/adapters/connection.py, gql/transport/common/adapters/websockets.py, gql/transport/common/base.py, gql/transport/exceptions.py, gql/transport/file_upload.py, gql/transport/httpx.py, gql/transport/phoenix_channel_websockets.py, gql/transport/requests.py, gql/transport/websockets.py, gql/transport/websockets_protocol.py, gql/utilities/get_introspection_query_ast.py, gql/utilities/parse_result.py, tests/conftest.py, tests/custom_scalars/test_money.py, tests/starwars/test_dsl.py, tests/test_aiohttp_multipart.py, tests/test_aiohttp_websocket_graphqlws_exceptions.py, tests/test_appsync_auth.py, tests/test_appsync_websockets.py, tests/test_phoenix_channel_subscription.py, tests/test_websocket_online.py.

# Acceptance criteria

- Given A GraphQL query with a deferred field, e.g., `{ hero { name ... @defer } }`, and a server that sends an initial payload with `data: { hero: { name: "R2-D2" } }` and `hasNext: true`, followed by an incremental payload with `incremental: [{ path: ["hero"], data: { homePlanet: "Naboo" } }]` and `hasNext: false`. When The client calls `session.execute_incremental(query)` and iterates over the yielded results. Then The first yielded result has `.data` equal to `{"hero": {"name": "R2-D2"}}` and `.has_next` equal to `True`. The second yielded result has `.data` equal to `{"hero": {"name": "R2-D2", "homePlanet": "Naboo"}}` and `.has_next` equal to `False`. The second result's `.data` includes both `name` and `homePlanet` because the incremental payload's data is merged into the accumulated data at the path `["hero"]`.
- Given A GraphQL query with a streamed list field, e.g., `{ hero { friends @stream } }`, and a server that sends an initial payload with `data: { hero: { friends: [] } }` and `hasNext: true`, followed by an incremental payload with `incremental: [{ path: ["hero", "friends", 0], items: ["Luke", "Leia"] }]` and `hasNext: false`. When The client calls `session.execute_incremental(query)` and iterates over the yielded results. Then .data of first yielded result is {'hero': {'friends': []}}; .data of second yielded result is {'hero': {'friends': ['Luke', 'Leia']}}; .has_next of first result is True; .has_next of second result is False.
- Given A GraphQL query with a deferred field, and a server that sends an initial payload with `data: { hero: { name: "R2-D2" } }` and `hasNext: true`, followed by an incremental payload with `incremental: [{ data: { extra: "value" } }]` (no `path` field) and `hasNext: false`. When The client calls `session.execute_incremental(query)` and iterates over the yielded results. Then The first yielded result has `.data` equal to `{"hero": {"name": "R2-D2"}}` and `.has_next` equal to `True`. The second yielded result has `.data` equal to `{"hero": {"name": "R2-D2"}, "extra": "value"}` and `.has_next` equal to `False`. The second result's `.data` includes the `extra` field at the root level because the incremental item has no `path` field, so it is treated as a root-level merge.
- Given A query with a deferred field. The server sends an incremental payload with no path field, containing data {"name": "Alice"}. When execute_incremental is called and iterated over. Then The .data attribute of the yielded object is {"name": "Alice"}, and the data is merged at the root level.
- Given An HTTP multipart transport (AIOHTTPTransport) is configured to connect to a server that supports the multipart/mixed content type with boundary=graphql and deferSpec=20220824. The server sends a multipart response with three parts: the first part contains a payload with data and hasNext: true, the second part contains a payload with an error (e.g., {"payload": {"errors": [{"message": "boom"}]}}), and the third part contains a payload with data and hasNext: false. When The client calls session.execute_incremental(query) and iterates over the yielded results. Then The iteration continues past the error part and yields a result for the third part. The first yielded result has .errors equal to None, the second yielded result has .errors equal to [{'message': 'boom'}], and the third yielded result has .errors equal to None.
- Given An HTTP multipart transport (AIOHTTPTransport) is configured to connect to a server that supports the multipart/mixed content type with boundary=graphql and deferSpec=20220824. The server sends a multipart response with two parts: the first part contains a payload with data and hasNext: true, and the second part contains a payload with an empty incremental array and hasNext: false (e.g., {"payload": {"incremental": [], "hasNext": false}}). When The client calls session.execute_incremental(query) and iterates over the yielded results. Then The client observes exactly 2 yielded results. The first result has the data from the first part and has_next=True. The second result is yielded even though it has no data or incremental fields, with has_next=False and the extensions from that specific payload (which may be empty/None).

Original requirements remain completion conditions when clarification is unresolved.

# Verification and completion

- Add or update relevant validations. Run applicable repository checks and report checks performed, failures and remaining ambiguity.
