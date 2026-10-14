"""Independent runtime probes derived from GRAPHQL_INSTRUCTION_PROBES.md."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any

import aiohttp.web
from gql import Client, GraphQLRequest
from gql.dsl import DSLFragment, DSLSchema
from gql.transport.aiohttp import AIOHTTPTransport
from gql.transport.websockets import WebsocketsTransport
from graphql import build_schema
from websockets.asyncio.server import serve


def run_payloads(payloads: Iterable[dict[str, Any]]) -> list[Any]:
    async def consume() -> list[Any]:
        parts = (
            b"".join(
                b"--graphql\r\nContent-Type: application/json\r\n\r\n"
                + json.dumps(payload).encode()
                + b"\r\n"
                for payload in payloads
            )
            + b"--graphql--\r\n"
        )

        async def handle(_request: aiohttp.web.Request) -> aiohttp.web.Response:
            return aiohttp.web.Response(
                body=parts,
                headers={
                    "Content-Type": (
                        "multipart/mixed; boundary=graphql; deferSpec=20220824"
                    )
                },
            )

        app = aiohttp.web.Application()
        app.router.add_post("/graphql", handle)
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        client = Client(
            transport=AIOHTTPTransport(
                url=f"http://127.0.0.1:{port}/graphql", timeout=5
            )
        )
        try:
            async with client as session:
                return [
                    result
                    async for result in session.execute_incremental(
                        GraphQLRequest("query { __typename }")
                    )
                ]
        finally:
            await runner.cleanup()

    return asyncio.run(consume())


def test_gql_01_accumulates_data_instead_of_returning_raw_delta() -> None:
    results = run_payloads(
        [
            {"data": {"user": {"id": "u1", "name": "old"}}, "hasNext": True},
            {
                "incremental": [{"path": ["user"], "data": {"name": "new"}}],
                "hasNext": False,
            },
        ]
    )
    assert results[1].data == {"user": {"id": "u1", "name": "new"}}


def test_gql_02_extensions_are_payload_local() -> None:
    results = run_payloads(
        [
            {"data": {"ok": True}, "extensions": {"trace": "first"}},
            {"incremental": [], "extensions": {"cost": 2}},
        ]
    )
    assert results[0].extensions == {"trace": "first"}
    assert results[1].extensions == {"cost": 2}


def test_gql_03_deferred_data_merges_and_overwrites_at_parent_path() -> None:
    results = run_payloads(
        [
            {"data": {"user": {"id": "u1", "name": "old"}}, "hasNext": True},
            {
                "incremental": [{"path": ["user"], "data": {"name": "new"}}],
                "hasNext": False,
            },
        ]
    )
    assert results[1].data["user"] == {"id": "u1", "name": "new"}


def test_gql_04_missing_path_merges_at_root() -> None:
    results = run_payloads(
        [
            {"data": {"left": 1}, "hasNext": True},
            {"incremental": [{"data": {"right": 2}}], "hasNext": False},
        ]
    )
    assert results[1].data == {"left": 1, "right": 2}


def test_gql_05_nested_path_navigates_list_indices() -> None:
    results = run_payloads(
        [
            {
                "data": {
                    "org": {"teams": [{"members": [{"id": "m1", "name": "old"}]}]}
                },
                "hasNext": True,
            },
            {
                "incremental": [
                    {
                        "path": ["org", "teams", 0, "members", 0],
                        "data": {"name": "new", "active": True},
                    }
                ],
                "hasNext": False,
            },
        ]
    )
    member = results[1].data["org"]["teams"][0]["members"][0]
    assert member == {"id": "m1", "name": "new", "active": True}


def test_gql_06_null_is_applied_as_a_value() -> None:
    results = run_payloads(
        [
            {"data": {"user": {"nickname": "Ada"}}, "hasNext": True},
            {
                "incremental": [{"path": ["user"], "data": {"nickname": None}}],
                "hasNext": False,
            },
        ]
    )
    assert "nickname" in results[1].data["user"]
    assert results[1].data["user"]["nickname"] is None


def test_gql_07_stream_items_start_at_the_given_append_index() -> None:
    results = run_payloads(
        [
            {"data": {"items": ["a"]}, "hasNext": True},
            {
                "incremental": [{"path": ["items", 1], "items": ["b", "c"]}],
                "hasNext": False,
            },
        ]
    )
    assert results[1].data["items"] == ["a", "b", "c"]


def test_gql_10_deferred_and_streamed_updates_survive_both_orders() -> None:
    initial = {"data": {"users": [{"id": "u1", "name": None}], "count": 0}}
    stream = {
        "incremental": [{"path": ["users", 1], "items": [{"id": "u2", "name": None}]}]
    }
    deferred = {"incremental": [{"path": ["users", 0], "data": {"name": "Ada"}}]}
    expected = {
        "users": [
            {"id": "u1", "name": "Ada"},
            {"id": "u2", "name": None},
        ],
        "count": 0,
    }

    observed = []
    for payload_order in ((stream, deferred), (deferred, stream)):
        results = run_payloads([initial, *payload_order])
        observed.append(results[-1].data)
    assert observed == [expected, expected]


def test_gql_11_each_yield_has_the_declared_result_attributes() -> None:
    results = run_payloads(
        [
            {"data": {"ok": True}, "hasNext": True},
            {"incremental": [], "hasNext": False},
        ]
    )
    assert len(results) == 2
    for result in results:
        assert hasattr(result, "data")
        assert hasattr(result, "has_next")
        assert hasattr(result, "errors")
        assert hasattr(result, "extensions")


def test_gql_12_non_incremental_payload_is_a_one_result_iteration() -> None:
    results = run_payloads([{"data": {"ok": True}}])
    assert len(results) == 1
    assert results[0].data == {"ok": True}


def test_gql_13_empty_incremental_array_still_yields() -> None:
    results = run_payloads([{"data": {"ok": True}, "incremental": [], "hasNext": True}])
    assert len(results) == 1
    assert results[0].data == {"ok": True}


def test_gql_14_has_next_only_payload_yields_and_iteration_continues() -> None:
    results = run_payloads(
        [
            {"data": {"before": True}, "hasNext": True},
            {"hasNext": True},
            {"data": {"after": True}, "hasNext": False},
        ]
    )
    assert len(results) == 3
    assert results[1].has_next is True
    assert results[2].has_next is False


def test_gql_15_errors_do_not_prevent_a_later_patch() -> None:
    error = {"message": "deferred field failed"}
    results = run_payloads(
        [
            {"data": {"user": {}}, "hasNext": True},
            {
                "errors": [error],
                "hasNext": True,
            },
            {
                "incremental": [{"path": ["user"], "data": {"name": "Ada"}}],
                "hasNext": False,
            },
        ]
    )
    assert len(results) == 3
    assert results[1].errors == [error]
    assert results[2].data == {"user": {"name": "Ada"}}


def test_gql_15b_incremental_item_errors_do_not_block_sibling_items() -> None:
    error = {"message": "deferred field failed"}
    results = run_payloads(
        [
            {"data": {"user": {}}, "hasNext": True},
            {
                "incremental": [
                    {"path": ["user"], "errors": [error]},
                    {"path": ["user"], "data": {"name": "Ada"}},
                ],
                "hasNext": False,
            },
        ]
    )
    assert results[1].data == {"user": {"name": "Ada"}}
    assert results[1].errors == [error]


def test_gql_16_http_multipart_delivers_incremental_payloads() -> None:
    async def exercise() -> list[Any]:
        parts = (
            b"--graphql\r\nContent-Type: application/json\r\n\r\n"
            b'{"data":{"user":{"id":"u1","name":"old"}},"hasNext":true}'
            b"\r\n--graphql\r\nContent-Type: application/json\r\n\r\n"
            b'{"incremental":[{"path":["user"],"data":{"name":"new"}}],'
            b'"hasNext":false}\r\n--graphql--\r\n'
        )

        async def handle(_request: aiohttp.web.Request) -> aiohttp.web.Response:
            return aiohttp.web.Response(
                body=parts,
                headers={
                    "Content-Type": (
                        "multipart/mixed; boundary=graphql; deferSpec=20220824"
                    )
                },
            )

        app = aiohttp.web.Application()
        app.router.add_post("/graphql", handle)
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        client = Client(
            transport=AIOHTTPTransport(
                url=f"http://127.0.0.1:{port}/graphql", timeout=5
            )
        )
        try:
            async with client as session:
                return [
                    result
                    async for result in session.execute_incremental(
                        GraphQLRequest("query { __typename }")
                    )
                ]
        finally:
            await runner.cleanup()

    results = asyncio.run(exercise())
    assert len(results) == 2
    assert results[0].data == {"user": {"id": "u1", "name": "old"}}
    assert results[1].data == {"user": {"id": "u1", "name": "new"}}


def test_gql_17_websocket_protocol_forwards_incremental_payloads() -> None:
    async def exercise() -> list[Any]:
        async def handler(websocket: Any) -> None:
            init_message = json.loads(await websocket.recv())
            assert init_message["type"] == "connection_init"
            await websocket.send(json.dumps({"type": "connection_ack"}))
            subscribe_message = json.loads(await websocket.recv())
            assert subscribe_message["type"] == "subscribe"
            request_id = subscribe_message["id"]
            await websocket.send(
                json.dumps(
                    {
                        "id": request_id,
                        "type": "next",
                        "payload": {
                            "data": {"user": {"id": "u1", "name": "old"}},
                            "hasNext": True,
                        },
                    }
                )
            )
            await websocket.send(
                json.dumps(
                    {
                        "id": request_id,
                        "type": "next",
                        "payload": {
                            "incremental": [
                                {"path": ["user"], "data": {"name": "new"}}
                            ],
                            "hasNext": False,
                        },
                    }
                )
            )
            await websocket.send(json.dumps({"id": request_id, "type": "complete"}))

        async with serve(
            handler, "127.0.0.1", 0, subprotocols=["graphql-transport-ws"]
        ) as server:
            port = server.sockets[0].getsockname()[1]
            client = Client(
                transport=WebsocketsTransport(
                    url=f"ws://127.0.0.1:{port}/graphql",
                    subprotocols=["graphql-transport-ws"],
                    ack_timeout=2,
                )
            )
            async with client as session:
                return [
                    result
                    async for result in session.execute_incremental(
                        GraphQLRequest("query { __typename }")
                    )
                ]

    results = asyncio.run(exercise())
    assert len(results) == 2
    assert results[0].data == {"user": {"id": "u1", "name": "old"}}
    assert results[1].data == {"user": {"id": "u1", "name": "new"}}


def _schema() -> DSLSchema:
    schema = build_schema(
        "type Query { users: [User!]!, user: User } "
        "type User { id: ID!, name: String! }"
    )
    return DSLSchema(schema)


def test_gql_18_defer_is_available_on_dsl_fragment() -> None:
    dsl = _schema()
    fragment = DSLFragment("UserFields").on(dsl.User).select(dsl.User.id)
    result = fragment.defer()
    assert result is fragment
    assert fragment.ast_field.directives[0].name.value == "defer"


def test_gql_19_defer_is_available_on_dsl_fragment_spread() -> None:
    dsl = _schema()
    fragment = DSLFragment("UserFields").on(dsl.User).select(dsl.User.id)
    spread = fragment.spread()
    result = spread.defer()
    assert result is spread
    assert spread.ast_field.directives[0].name.value == "defer"


def test_gql_20_stream_supports_all_optional_argument_combinations() -> None:
    dsl = _schema()
    cases = (
        ({}, None),
        ({"label": "batch"}, {"label": "batch"}),
        ({"initial_count": 2}, {"initialCount": "2"}),
        (
            {"label": "batch", "initial_count": 2},
            {"label": "batch", "initialCount": "2"},
        ),
    )
    for kwargs, expected_args in cases:
        field = dsl.Query.users.stream(**kwargs)
        directive = field.ast_field.directives[-1]
        actual_args = {
            argument.name.value: getattr(argument.value, "value", None)
            for argument in directive.arguments
        }
        assert directive.name.value == "stream"
        if expected_args is not None:
            assert all(
                actual_args.get(key) == value for key, value in expected_args.items()
            )
