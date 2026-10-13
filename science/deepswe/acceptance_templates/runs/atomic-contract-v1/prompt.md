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

- Given A GraphQL client using the gql library with an HTTP transport (AIOHTTPTransport) connected to a server that supports the incremental delivery protocol (multipart/mixed; boundary=graphql; deferSpec=20220824). The client has a schema that includes the @defer and @stream directives as defined in the context. The client is configured with parse_results=True and serialize_variables=True. Query: `query ($id: ID!, $first: Int!) {  node(id: $id) {    ... on User @defer(label: "userDetails") {      name      friends(first: $first) @stream(initialCount: 1, label: "friendsStream") {        id      }    }  }}`. When The client executes a query that uses @defer on an inline fragment and @stream on a list field, and the server sends a sequence of multipart parts: an initial payload with data and hasNext: true, then a subsequent payload with an incremental item for the deferred field, then a payload with an incremental item for the streamed field, and finally a payload with hasNext: false and no data or incremental fields. Then The async generator yields an ExecutionResult object for each multipart part. The first yielded result has data equal to the initial payload's data, hasNext equal to the initial payload's hasNext value, errors equal to the initial payload's errors, and extensions equal to the initial payload's extensions. The second yielded result has data equal to the initial payload's data merged with the deferred field's incremental item at the path given by the incremental item's path, hasNext equal to the second payload's hasNext value, errors equal to the second payload's errors, and extensions equal to the second payload's extensions. The third yielded result has data equal to the previous accumulated data merged with the streamed field's incremental item at the path given by the incremental item's path, hasNext equal to the third payload's hasNext value, errors equal to the third payload's errors, and extensions equal to the third payload's extensions. The fourth yielded result has data equal to the previous accumulated data, hasNext equal to the fourth payload's hasNext value (false), errors equal to the fourth payload's errors, and extensions equal to the fourth payload's extensions.
- Given GraphQL schema with a list field 'items' of type [Item] on Query, where Item has scalar fields 'id' and 'name'. The schema supports @stream directive on list fields. Query: `query StreamItems { items @stream(initialCount: 1) { id name } }`. When A client sends a GraphQL query with @stream on the 'items' field, and the server responds with an incremental payload that contains an 'items' array in the 'incremental' field. Then the incremental payload's 'incremental' field contains an 'items' array.
- Given A GraphQL schema with a Query type exposing a field `search` that returns a `SearchResult` object containing a list of `Item` objects. Each `Item` has fields `id` (ID!), `name` (String!), and `description` (String). The schema also includes a `slowField` on `Item` that is expensive to resolve. The server supports the `@defer` and `@stream` directives as per the specification. Query: `query SearchQuery($searchTerm: String!, $first: Int!) {
  search(term: $searchTerm) {
    items(first: $first) @stream(initialCount: 1, label: "itemsStream") {
      id
      name
      ... on Item @defer(label: "itemDescription") {
        description
      }
    }
  }
} `. When A client sends a GraphQL query that requests both a deferred field and a streamed list field concurrently, using the HTTP multipart transport with `boundary=graphql` and `deferSpec=20220824`. Then The client receives an initial HTTP multipart payload with `content-type: multipart/mixed; boundary="graphql"; deferSpec="20220824"` containing a JSON body with `data` equal to `{"search": {"items": [{"id": "1", "name": "Item 1"}]}}`, `hasNext` equal to `true`, and `incremental` absent. Subsequent payloads in the same multipart stream include one payload with `incremental` containing an item with `path` equal to `["search", "items", 1]`, `items` equal to `[{"id": "2", "name": "Item 2"}]`, and `label` equal to `"itemsStream"`, and another payload with `incremental` containing an item with `path` equal to `["search", "items", 0, "description"]`, `data` equal to `{"description": "Deferred description"}`, and `label` equal to `"itemDescription"`. The order of these two subsequent payloads is not fixed; they may arrive in either order. Each yielded result object from `session.execute_incremental` has `.data` accumulated across all payloads received so far, `.has_next` equal to `true` until the final payload where it is `false`, `.errors` equal to `None` if no errors occurred, and `.extensions` equal to the `extensions` field from that specific payload (or `None` if absent). The final payload has `hasNext` equal to `false` and no `incremental` field.
- Given A GraphQL server that supports @defer and @stream directives, with a schema containing a Query type with a field 'allUsers' returning a list of User objects, each with 'id' and 'name' fields. The server is configured to return non-incremental responses when no @defer or @stream directives are present in the query. Query: `query GetUsers { allUsers { id name } }`. When A client sends a standard GraphQL query without any @defer or @stream directives, using the HTTP transport with a POST request to the /graphql endpoint. Then The client sends a standard GraphQL query without any @defer or @stream directives, using the HTTP transport with a POST request to the /graphql endpoint. The server, configured to return non-incremental responses when no such directives are present, responds with a single JSON payload containing the full result of the query. The client's execute method receives this response and returns an ExecutionResult object with .data containing the complete allUsers list, .errors as None, and .extensions as None. No incremental payloads are generated, and no additional results are yielded.
- Given A GraphQL schema with a Query type exposing a field `viewer` of type `User`, where `User` has fields `id: ID!`, `name: String!`, and `friends: [User!]!`. The server supports the `@defer` directive on fragment spreads and inline fragments, and the `@stream` directive on list fields. The client uses the `gql` DSL to construct queries. Query: `query {
  viewer {
    id
    name
    ...UserFields @defer(label: "userFields")
    ... on User @defer(label: "inlineUser") {
      friends @stream(initialCount: 1, label: "friendsStream") {
        id
        name
      }
    }
  }
}

fragment UserFields on User {
  id
  name
} `. When The client constructs a query using the DSL, applying `.defer()` to both a `DSLFragment` and a `DSLFragmentSpread`, and `.stream()` to a list field, then executes it via `session.execute_incremental(query)`. Then The client constructs a query using the DSL, applying `.defer()` to both a `DSLFragment` and a `DSLFragmentSpread`, and `.stream()` to a list field, then executes it via `session.execute_incremental(query)`. The required observation is that the DSL must support `.defer()` on both `DSLFragment` and `DSLFragmentSpread` instances, allowing the `@defer` directive to be applied to fragment spreads and inline fragments in the constructed query.

Original requirements remain completion conditions when clarification is unresolved.

# Verification and completion

- Add or update validations for the requirements and observable conditions above. Report checks performed, failures, compatibility changes, and unresolved behavior.
