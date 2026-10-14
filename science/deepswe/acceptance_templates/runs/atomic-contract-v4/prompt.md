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

- Given A GraphQL schema with a Query type exposing a field `hero` returning a `Character` object with `id`, `name`, and `friends` (a list of `Character`). The schema supports the `@defer` directive on inline fragments and fragment spreads, and `@stream` on list fields. The server implements incremental delivery over HTTP multipart (boundary=graphql, deferSpec=20220824). Query: `query { hero { id ... on Character @defer(label: "name") { name } friends @stream(initialCount: 1, label: "friends") { id } } }`. When A client sends a query with a deferred inline fragment and a streamed list field, and the server responds with an initial payload followed by incremental payloads. Then The async generator must yield result objects.
- Given A GraphQL schema with a query type exposing a field `allProducts` that returns a list of `Product` objects. Each `Product` has fields `id: ID!`, `name: String!`, and `reviews: [Review]` where `Review` has `body: String!`. The server supports incremental delivery via HTTP multipart with `boundary=graphql` and `deferSpec=20220824`. Query: `query {
  allProducts {
    id
    name
    reviews @stream(initialCount: 0) {
      body
    }
  }
}`. When A client sends a query that requests `allProducts` with a `@stream` directive on the `reviews` field, and the server responds with multiple payloads: first a payload containing the initial data (with `reviews` as an empty list), then a payload with an incremental item that has a `path` of `["allProducts", 0, "reviews"]` and an `items` array containing one review object, and finally a payload with `hasNext: false` and no `data` or `incremental` fields. Then The .data attribute of the second yielded result object contains the accumulated data from the first payload (with `allProducts` as a list containing one product object with `id`, `name`, and `reviews` as an empty list) merged with the incremental item's `items` array inserted at index 0 of the `reviews` list of the product at index 0 in `allProducts`, resulting in that product's `reviews` list containing the one review object with its `body` field. The .data attribute of the third yielded result object is identical to the .data of the second yielded result object, as the third payload contains no `data` or `incremental` fields to modify it.
- Given A GraphQL schema with a Query type exposing a `books` field returning a list of `Book` objects, each having `id`, `title`, and `author` fields. The server supports the `@defer` and `@stream` directives as per the specification. Query: `query {
  books {
    id
    title
    ... on Book @defer(label: "bookDetails") {
      author
    }
    reviews @stream(initialCount: 1, label: "reviewsStream") {
      id
      text
    }
  }
} `. When A client sends a GraphQL query that concurrently defers a fragment and streams a list field, using the HTTP multipart transport with `boundary=graphql` and `deferSpec=20220824`. Then The client receives a multipart response with boundary=graphql and deferSpec=20220824. The first payload contains the initial data with the books list, each book having id, title, and the first review (due to initialCount: 1) under the reviews field, but no author field. Subsequent payloads include: (1) a payload with path ["books", 0, "author"] and data {"author": "<author_value>"} for the deferred fragment, and (2) payloads with path ["books", 0, "reviews", 1], ["books", 0, "reviews", 2], etc., each with an items array containing one review object, incrementing the insertion index. The client accumulates these into the .data attribute, merging the deferred author into the corresponding book object and inserting streamed reviews at the specified indices. Each yielded result object has .data, .has_next, .errors, and .extensions attributes, where .extensions reflects only that payload's extensions. The final payload has has_next: false. The observation is that the client receives and processes both deferred and streamed fields concurrently, with the deferred fragment's data merged into the parent book object and the streamed reviews inserted into the reviews list at the correct positions, all within the same multipart response stream.
- Given A GraphQL server that supports @defer and @stream directives, and a client using the gql library with the AIOHTTPTransport over HTTP multipart (boundary=graphql, deferSpec=20220824). The server is configured to return a non-incremental response for a query that includes a @defer directive, meaning the server ignores the directive and returns a single JSON response with the full data. Query: `query { hero { name ... @defer { friends { name } } } }`. When The client executes the query using session.execute_incremental(query) and iterates over the async generator. Then The async generator yields exactly one result object. That result has .data equal to the full data from the single JSON response, .has_next equal to False, .errors equal to the errors from the single JSON response (or None if absent), and .extensions equal to the extensions from the single JSON response (or None if absent). The .data is not accumulated from multiple payloads because only one payload exists. The .extensions is from that single payload, not accumulated. No incremental items are processed because the response is non-incremental. The generator completes after yielding this single result.
- Given A GraphQL schema with a Query type exposing a list field `searchResults` of type `[SearchResult!]!`, where `SearchResult` has scalar fields `id` and `title`. The schema includes the `@stream` directive definition with arguments `if: Boolean!`, `label: String`, and `initialCount: Int`. Query: `query { searchResults @stream(label: "stream1", initialCount: 2) { id title } }`. When Constructing a DSL query using the `gql.dsl` module, the user calls `.stream()` on the `searchResults` field with `label='stream1'` and `initial_count=2`. Then the DSL field's AST directives tuple contains a DirectiveNode with name 'stream' and arguments label='stream1' and initialCount=2.

Original requirements remain completion conditions when clarification is unresolved.

# Verification and completion

- Add or update validations for the requirements and observable conditions above. Report checks performed, failures, compatibility changes, and unresolved behavior.
