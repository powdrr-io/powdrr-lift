# Objective

Add @defer and @stream directive support so servers can send critical data first while deferring or streaming non-essential fields incrementally.

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
- AsyncTransport.subscribe (gql/transport/async_transport.py:52) is an abstract method returning AsyncGenerator[ExecutionResult, None], and AIOHTTPTransport.subscribe (gql/transport/aiohttp.py:425) implements it by yielding ExecutionResult objects from multipart responses. (Source: gql/transport/async_transport.py:52, gql/transport/aiohttp.py:425.)
- SubscriptionTransportBase.subscribe (gql/transport/common/base.py:267) yields ExecutionResult objects and handles cleanup via _remove_listener in its finally block. (Source: gql/transport/common/base.py:267.)
- The existing TransportQueryError constructor accepts errors, data, and extensions parameters, which are the same fields required on the new yielded result objects. (Source: gql/transport/exceptions.py:41.)
- The existing execute_batch methods on HTTPXTransport (gql/transport/httpx.py:274) and HTTPXAsyncTransport (gql/transport/httpx.py:371) return List[ExecutionResult] and are not affected by incremental delivery; they remain the contract for batch execution. (Source: gql/transport/httpx.py:274, gql/transport/httpx.py:371.)
- WebsocketsProtocolTransportBase._parse_answer_graphqlws (gql/transport/websockets_protocol.py:259) parses 'next' messages into ExecutionResult with data, errors, and extensions; this remains the contract for WebSocket incremental payload forwarding. (Source: gql/transport/websockets_protocol.py:259.)
- SubscriptionTransportBase._handle_answer (gql/transport/common/base.py:252) is the existing contract for forwarding answers to listeners; it puts (answer_type, execution_result) into the listener queue, and subclasses like WebsocketsProtocolTransportBase._handle_answer (gql/transport/websockets_protocol.py:458) extend it to handle ping/pong, so incremental payloads must be forwarded through this existing mechanism. (Source: gql/transport/common/base.py:252, gql/transport/websockets_protocol.py:458.)
- AIOHTTPTransport._parse_multipart_response (gql/transport/aiohttp.py:489) yields ExecutionResult objects from multipart parts, and its handling of empty parts and EOF must remain compatible for incremental delivery. (Source: gql/transport/aiohttp.py:489.)
- WebsocketsTransport and AIOHTTPWebsocketsTransport both extend WebsocketsProtocolTransportBase, which initializes subprotocols to both APOLLO_SUBPROTOCOL and GRAPHQLWS_SUBPROTOCOL by default, and this protocol negotiation must remain intact for WebSocket incremental delivery. (Source: gql/transport/websockets.py:10, gql/transport/aiohttp_websockets.py:11, gql/transport/websockets_protocol.py:35.)
- WebsocketsTransport.__init__ (gql/transport/websockets.py:18) and AIOHTTPWebsocketsTransport.__init__ (gql/transport/aiohttp_websockets.py:19) both accept subprotocols, init_payload, and various timeouts; they delegate to WebsocketsProtocolTransportBase, preserving the existing protocol negotiation and payload handling. (Source: gql/transport/websockets.py:18, gql/transport/aiohttp_websockets.py:19.)
- DSLFragmentSpread.directives (gql/dsl.py:1335) and DSLField.directives (gql/dsl.py:1182) both call super().directives() and then assign self.ast_field.directives = self.directives_ast, ensuring AST directives are updated; this pattern is relevant for adding .defer() and .stream() methods that must also update the AST. (Source: gql/dsl.py:1335, gql/dsl.py:1182.)

# Relevant context

- AsyncClientSession.subscribe (gql/client.py:1395) is an async generator yielding either Dict[str, Any] or ExecutionResult, with get_execution_result controlling the yield type. (Source: gql/client.py:1395.)
- AsyncClientSession.execute_batch and SyncClientSession.execute_batch are the existing batch execution APIs, each returning either a list of data dicts or a list of ExecutionResult objects depending on get_execution_result. (Source: gql/client.py:1688, gql/client.py:1109.)
- ParseResultVisitor.enter_field (gql/utilities/parse_result.py:170) handles list traversal by recursively visiting each item and returns a dict with the field name as key, preserving the existing result shape for non-incremental responses. (Source: gql/utilities/parse_result.py:170.)
- ParseResultVisitor.enter_field (gql/utilities/parse_result.py:170) is the existing API that traverses result data and currently returns REMOVE when current_result is None (line 186-188), which is the contract for handling null values in nested paths. (Source: gql/utilities/parse_result.py:170.)
- AIOHTTPTransport._parse_multipart_part (gql/transport/aiohttp.py:532) currently returns None for empty bodies and heartbeats, and raises TransportServerError for transport-level errors, which must be preserved for non-incremental multipart handling. (Source: gql/transport/aiohttp.py:532.)
- WebsocketsProtocolTransportBase.__init__ (gql/transport/websockets_protocol.py:35) accepts adapter, init_payload, timeouts, ping/pong settings, answer_pings, and subprotocols; it initializes payloads dict and ping/pong events. (Source: gql/transport/websockets_protocol.py:35.)

# Acceptance criteria


Original requirements remain completion conditions when clarification is unresolved.

# Verification and completion

- Add or update validations for the requirements and observable conditions above. Report checks performed, failures, compatibility changes, and unresolved behavior.
