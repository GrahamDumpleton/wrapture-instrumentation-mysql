"""What the instrumentation records: the connect in each of its
spellings and from a pool, the execute family, stored procedures, the
transaction boundaries, the cursor subclasses, and what stays out of
capture; every case a coroutine run to completion inside the test."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Iterator
from typing import Any

import pytest

pytest.importorskip("aiomysql")

import aiomysql
import aiomysql.cursors
from wrapture import Event, Tape, instrumentation, timeline

from tests.aiomysql.conftest import connect_kwargs
from tests.conftest import Server
from wrapture_instrumentation_mysql.aiomysql import AiomysqlInstrumentation

CONNECT = "aiomysql.connection:Connection._connect"
EXECUTE = "aiomysql.cursors:Cursor.execute"
EXECUTEMANY = "aiomysql.cursors:Cursor.executemany"
CALLPROC = "aiomysql.cursors:Cursor.callproc"


def run(coroutine: Awaitable[Any]) -> Any:
    return asyncio.run(coroutine)  # type: ignore[arg-type]


def at(tape: Tape, path: str) -> list[Event]:
    return [event for event in tape.all if event.path == path]


def recorded(tape: Tape) -> str:
    """Everything the tape holds, for asserting what never appears."""

    return repr(
        [
            (event.path, event.label, event.data, event.arguments, event.result)
            for event in tape.all
        ]
    )


def contract(event: Event, mysql: Server, operation: str) -> None:
    assert event.category == "database"
    assert event.data["system"] == "mysql"
    assert event.data["operation"] == operation
    assert event.data["database"] == mysql.database
    assert event.data["host"] == mysql.host
    assert event.data["port"] == mysql.port


@pytest.fixture
def procedure(mysql: Server) -> Iterator[str]:
    """A stored procedure with a name of its own (procedures cannot be
    temporary), doubling its one argument; dropped afterwards.

    Its own connection and CREATE would record on a tape already
    running, so a test takes this fixture ahead of `tape` in its
    argument list, which is the order pytest sets them up in.
    """

    name = f"wrapture_double_{os.getpid()}"

    async def create() -> None:
        async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(f"CREATE PROCEDURE {name}(IN x INT) SELECT x * 2")

    async def drop() -> None:
        async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(f"DROP PROCEDURE IF EXISTS {name}")

    run(create())
    try:
        yield name
    finally:
        run(drop())


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def test_connect_records_a_database_leaf(mysql: Server, tape: Tape) -> None:
    async def scenario() -> None:
        connection = await aiomysql.connect(**connect_kwargs(mysql))
        connection.close()

    run(scenario())

    (event,) = at(tape, CONNECT)
    contract(event, mysql, "CONNECT")
    assert event.label is None
    assert event.arguments is None
    assert event.result is None
    assert tape.children_of(event) == []


def test_every_spelling_of_connect_records_once(mysql: Server, tape: Tape) -> None:
    # Awaited, entered, or built and connected by hand: the open is the
    # connection's own _connect either way.

    async def scenario() -> None:
        connection = await aiomysql.connect(**connect_kwargs(mysql))
        connection.close()

        async with aiomysql.connect(**connect_kwargs(mysql)):
            pass

        built = aiomysql.Connection(**connect_kwargs(mysql))
        await built._connect()
        built.close()

    run(scenario())

    assert len(at(tape, CONNECT)) == 3
    assert len(tape.all) == 3


def test_connect_never_captures_its_arguments(mysql: Server, tape: Tape) -> None:
    async def scenario() -> None:
        async with aiomysql.connect(**connect_kwargs(mysql)):
            pass

    run(scenario())

    # The factory's arguments carry the password, and none of them
    # pass through the bound method, which takes no arguments at all.

    (event,) = at(tape, CONNECT)
    assert event.arguments is None
    assert "password" not in recorded(tape)


def test_a_refused_connection_records_its_error_and_never_the_password(
    mysql: Server, tape: Tape
) -> None:
    async def scenario() -> None:
        with pytest.raises(aiomysql.OperationalError):
            await aiomysql.connect(
                **{**connect_kwargs(mysql), "password": "wrong-hunter2"}
            )

    run(scenario())

    (event,) = at(tape, CONNECT)
    contract(event, mysql, "CONNECT")
    assert isinstance(event.exception, aiomysql.OperationalError)
    assert "wrong-hunter2" not in recorded(tape)


def test_a_reconnecting_ping_records_a_connect(mysql: Server, tape: Tape) -> None:
    async def scenario() -> None:
        connection = await aiomysql.connect(**connect_kwargs(mysql))
        connection.close()
        await connection.ping(reconnect=True)
        connection.close()

    run(scenario())

    assert len(at(tape, CONNECT)) == 2


def test_the_statements_connect_runs_itself_fold_into_the_connect(
    mysql: Server, tape: Tape
) -> None:
    # The driver runs the sql_mode and the init_command through its
    # raw query while connecting, and commits after the latter; all of
    # it happens beneath the CONNECT leaf.

    async def scenario() -> None:
        connection = await aiomysql.connect(
            sql_mode="TRADITIONAL",
            init_command="SET @wrapture = 1",
            **connect_kwargs(mysql),
        )
        connection.close()

    run(scenario())

    (event,) = tape.all
    assert event.path == CONNECT
    assert "statement" not in event.data


def test_a_connection_from_a_pool_records(mysql: Server, tape: Tape) -> None:
    # The pool opens its connections through the same factory, on the
    # task that asked for one, so the open records like any other and
    # the pooled connection is the real class, its queries recording
    # through the bindings; taking and returning it record nothing.

    async def scenario() -> None:
        pool = await aiomysql.create_pool(minsize=1, maxsize=2, **connect_kwargs(mysql))
        try:
            async with pool.acquire() as connection:
                assert type(connection) is aiomysql.Connection
                async with connection.cursor() as cursor:
                    await cursor.execute("SELECT 1")
                    assert await cursor.fetchone() == (1,)
        finally:
            pool.close()
            await pool.wait_closed()

    run(scenario())

    assert [event.path for event in tape.all] == [CONNECT, EXECUTE]


# ---------------------------------------------------------------------------
# the execute family
# ---------------------------------------------------------------------------


def test_execute_records_operation_but_no_statement(mysql: Server, tape: Tape) -> None:
    async def scenario() -> None:
        async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("CREATE TEMPORARY TABLE secrets (value TEXT)")
                await cursor.execute("INSERT INTO secrets VALUES (%s)", ("hunter2",))

    run(scenario())

    create, insert = at(tape, EXECUTE)
    contract(create, mysql, "CREATE")
    contract(insert, mysql, "INSERT")

    for event in (create, insert):
        assert "statement" not in event.data

    # The SQL reduces to its length and the parameters to a count in
    # the captured arguments, so the value never reaches the record.

    assert insert.arguments is not None
    assert insert.arguments["query"] == "<31 chars>"
    assert insert.arguments["args"] == "<1 values>"
    assert insert.result == 1
    assert "hunter2" not in recorded(tape)


def test_the_statement_setting_records_the_template_with_its_placeholders(
    mysql: Server,
) -> None:
    async def scenario() -> None:
        async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("CREATE TEMPORARY TABLE items (name TEXT)")
                await cursor.execute(
                    "INSERT INTO items VALUES (%(name)s)", {"name": "widget"}
                )
                await cursor.execute(
                    "SELECT name FROM items WHERE name = %s", ("widget",)
                )
                assert await cursor.fetchall() == (("widget",),)

    with instrumentation(AiomysqlInstrumentation, statement=True), timeline() as tape:
        run(scenario())

    create, insert, select = at(tape, EXECUTE)
    assert create.data["statement"] == "CREATE TEMPORARY TABLE items (name TEXT)"
    assert create.data["operation"] == "CREATE"

    # The bindings sit above the driver's interpolation, so the text
    # is the template, never the statement with the values in it.

    assert insert.data["statement"] == "INSERT INTO items VALUES (%(name)s)"
    assert insert.arguments is not None
    assert insert.arguments["args"] == "<1 values>"
    assert select.data["statement"] == "SELECT name FROM items WHERE name = %s"
    assert "widget" not in recorded(tape)


def test_executemany_records_one_event_and_never_iterates_a_generator(
    mysql: Server, tape: Tape
) -> None:
    async def scenario() -> None:
        async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("CREATE TEMPORARY TABLE items (name TEXT)")
                await cursor.executemany(
                    "INSERT INTO items VALUES (%s)", [("a",), ("b",)]
                )
                await cursor.executemany(
                    "INSERT INTO items VALUES (%s)", (("g",) for _ in range(3))
                )
                await cursor.execute("SELECT count(*) FROM items")
                assert await cursor.fetchone() == (5,)

    run(scenario())

    listed, generated = at(tape, EXECUTEMANY)
    contract(listed, mysql, "INSERT")
    assert listed.arguments is not None
    assert listed.arguments["args"] == "<2 values>"
    assert listed.result == 2
    assert generated.arguments is not None
    assert generated.arguments["args"] == "<generator>"

    # The multi-row INSERT the driver assembles goes through its own
    # execute beneath the leaf, so the only execute events are the
    # test's own two.

    assert [event.data["operation"] for event in at(tape, EXECUTE)] == [
        "CREATE",
        "SELECT",
    ]


def test_executemany_of_a_non_insert_records_once_not_per_row(
    mysql: Server, tape: Tape
) -> None:
    # Anything but a multi-row INSERT the driver runs once per
    # parameter set through execute; the leaf still makes it one event.

    async def scenario() -> None:
        async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "CREATE TEMPORARY TABLE items (name TEXT, seen INT)"
                )
                await cursor.executemany(
                    "INSERT INTO items VALUES (%s, 0)", [("a",), ("b",)]
                )
                await cursor.executemany(
                    "UPDATE items SET seen = 1 WHERE name = %s", [("a",), ("b",)]
                )

    run(scenario())

    (insert, update) = at(tape, EXECUTEMANY)
    contract(update, mysql, "UPDATE")
    assert update.result == 2
    assert len(at(tape, EXECUTE)) == 1


def test_callproc_records_a_call_with_the_procedure(
    mysql: Server, procedure: str, tape: Tape
) -> None:
    async def scenario() -> None:
        async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
            async with connection.cursor() as cursor:
                await cursor.callproc(procedure, (21,))
                assert await cursor.fetchone() == (42,)

    run(scenario())

    (event,) = at(tape, CALLPROC)
    contract(event, mysql, "CALL")
    assert event.data["procedure"] == procedure
    assert "statement" not in event.data

    # The procedure's name is captured as a name; its arguments reduce
    # to a count. The SET of the session variable and the CALL itself
    # go straight to the wire beneath the leaf, so nothing else is
    # recorded.

    assert event.arguments is not None
    assert event.arguments["procname"] == procedure
    assert event.arguments["args"] == "<1 values>"
    assert event.result == "<tuple>"
    assert [event.path for event in tape.all] == [CONNECT, CALLPROC]


def test_a_failing_query_records_its_exception(mysql: Server, tape: Tape) -> None:
    async def scenario() -> None:
        async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
            async with connection.cursor() as cursor:
                with pytest.raises(aiomysql.ProgrammingError):
                    await cursor.execute("SELECT nope FROM nowhere")

    run(scenario())

    (event,) = at(tape, EXECUTE)
    contract(event, mysql, "SELECT")
    assert isinstance(event.exception, aiomysql.ProgrammingError)


@pytest.mark.parametrize(
    "cursor_class",
    [
        aiomysql.cursors.DictCursor,
        aiomysql.cursors.SSCursor,
        aiomysql.cursors.SSDictCursor,
    ],
    ids=lambda cls: str(cls.__name__),
)
def test_every_cursor_class_records_through_the_base_binding(
    mysql: Server, tape: Tape, cursor_class: type
) -> None:
    async def scenario() -> Any:
        async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
            async with connection.cursor(cursor_class) as cursor:
                await cursor.execute("SELECT %s AS token", ("s3cret-token",))
                return await cursor.fetchall()

    rows = run(scenario())
    assert list(rows) and "s3cret-token" in repr(rows)

    # The subclasses inherit execute, so the event's path names the
    # base class whichever ran; the value still never reaches the
    # record.

    (event,) = at(tape, EXECUTE)
    contract(event, mysql, "SELECT")
    assert "s3cret-token" not in recorded(tape)


def test_the_raw_query_path_is_not_recorded(mysql: Server, tape: Tape) -> None:
    async def scenario() -> None:
        connection = await aiomysql.connect(**connect_kwargs(mysql))
        try:
            await connection.query("SELECT 1")
            assert connection.affected_rows() == 1
        finally:
            connection.close()

    run(scenario())

    assert [event.path for event in tape.all] == [CONNECT]


# ---------------------------------------------------------------------------
# transaction boundaries
# ---------------------------------------------------------------------------


def test_begin_commit_and_rollback_record_their_operations(
    mysql: Server, tape: Tape
) -> None:
    async def scenario() -> None:
        async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("CREATE TEMPORARY TABLE items (name TEXT)")

                await connection.begin()
                await cursor.execute("INSERT INTO items VALUES ('kept')")
                await connection.commit()

                await connection.begin()
                await cursor.execute("INSERT INTO items VALUES ('dropped')")
                await connection.rollback()

                await cursor.execute("SELECT name FROM items")
                assert await cursor.fetchall() == (("kept",),)

    run(scenario())

    (begin, _) = at(tape, "aiomysql.connection:Connection.begin")
    contract(begin, mysql, "BEGIN")
    (commit,) = at(tape, "aiomysql.connection:Connection.commit")
    contract(commit, mysql, "COMMIT")
    (rollback,) = at(tape, "aiomysql.connection:Connection.rollback")
    contract(rollback, mysql, "ROLLBACK")

    for event in (begin, commit, rollback):
        assert event.arguments == {}
        assert event.result is None


def test_the_connection_context_manager_records_nothing_on_exit(
    mysql: Server, tape: Tape
) -> None:
    # Leaving the block closes the connection and performs no commit
    # or rollback, so there is no boundary to record.

    async def scenario() -> Any:
        async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1")
        return connection

    connection = run(scenario())

    assert connection.closed
    assert [event.path for event in tape.all] == [CONNECT, EXECUTE]


def test_autocommit_is_not_recorded(mysql: Server, tape: Tape) -> None:
    async def scenario() -> None:
        async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
            await connection.autocommit(True)
            assert connection.get_autocommit() is True

    run(scenario())

    assert [event.path for event in tape.all] == [CONNECT]


# ---------------------------------------------------------------------------
# the shape of every event
# ---------------------------------------------------------------------------


def test_every_event_carries_the_contract_keys(
    mysql: Server, procedure: str, tape: Tape
) -> None:
    async def scenario() -> None:
        async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("CREATE TEMPORARY TABLE items (name TEXT)")
                await connection.begin()
                await cursor.executemany(
                    "INSERT INTO items VALUES (%s)", [("a",), ("b",)]
                )
                await connection.commit()
                await cursor.callproc(procedure, (1,))
                await cursor.fetchall()

                # A procedure call leaves an extra, empty result set
                # behind its own, which the caller steps past before
                # the connection takes another command.

                while await cursor.nextset():
                    pass

                await connection.rollback()

    run(scenario())

    assert len(tape.all) == 7

    keys: set[str] = {"system", "operation", "database", "host", "port"}
    for event in tape.all:
        assert keys <= set(event.data), event.path
        assert event.category == "database", event.path

    data: dict[str, Any] = tape.all[0].data
    assert data["system"] == "mysql"
