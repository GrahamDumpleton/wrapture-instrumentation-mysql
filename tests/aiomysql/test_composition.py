"""With the core package's sqlalchemy instrumentation applied
alongside, over the aiomysql dialect and the async engine: the
default leaf keeps the driver's events out of the tree, leaf off
nests them beneath each statement, and raw driver use beside the
engine still records."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("aiomysql")
pytest.importorskip("sqlalchemy")
pytest.importorskip("wrapture_instrumentation")

import aiomysql
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from wrapture import Event, Tape, instrumentation, timeline
from wrapture_instrumentation.database.sqlalchemy import SQLAlchemyInstrumentation

from tests.aiomysql.conftest import connect_kwargs
from tests.conftest import Server
from wrapture_instrumentation_mysql.aiomysql import AiomysqlInstrumentation

EXECUTE = "sqlalchemy.engine.default:DefaultDialect.do_execute"
CONNECT = "sqlalchemy.engine.default:DefaultDialect.connect"
COMMIT = "sqlalchemy.engine.base:Connection._commit_impl"

# The statements as SQLAlchemy is handed them, and as the driver
# then sees them (a named parameter compiles to the driver's
# spelling).

WORKLOAD = [
    (
        "CREATE TEMPORARY TABLE items (name TEXT)",
        "CREATE TEMPORARY TABLE items (name TEXT)",
    ),
    ("INSERT INTO items VALUES (:name)", "INSERT INTO items VALUES (%s)"),
    ("SELECT name FROM items", "SELECT name FROM items"),
]
DRIVER_STATEMENTS = [driver for _, driver in WORKLOAD]


def at(tape: Tape, path: str) -> list[Event]:
    return [event for event in tape.all if event.path == path]


def driver_events(tape: Tape) -> list[Event]:
    """The aiomysql events that belong to the workload, leaving out
    the dialect's own setup queries and the pool's reset-on-return
    rollback, which run outside SQLAlchemy's seams."""

    return [
        event
        for event in tape.all
        if event.path.startswith("aiomysql.")
        and event.data.get("statement") in DRIVER_STATEMENTS
    ]


def engine_url(mysql: Server) -> str:
    return mysql.url.replace("mysql://", "mysql+aiomysql://")


async def workload(mysql: Server) -> None:
    engine = create_async_engine(engine_url(mysql))

    async with engine.begin() as connection:
        await connection.execute(text(WORKLOAD[0][0]))
        await connection.execute(text(WORKLOAD[1][0]), {"name": "a"})
        (await connection.execute(text(WORKLOAD[2][0]))).fetchall()

    await engine.dispose()


def test_the_default_leaf_keeps_the_driver_out(mysql: Server) -> None:
    with (
        instrumentation(AiomysqlInstrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation),
        timeline() as tape,
    ):
        asyncio.run(workload(mysql))

    # The dialect's seams record the workload (and the dialect's own
    # setup queries beside it, which are do_execute calls too).

    assert len(at(tape, EXECUTE)) >= 3
    assert len(at(tape, CONNECT)) == 1
    assert len(at(tape, COMMIT)) == 1

    # Every workload statement ran beneath a sqlalchemy leaf, so the
    # driver's own events for them stay out of the tape.

    assert driver_events(tape) == []
    assert at(tape, "aiomysql.connection:Connection._connect") == []
    assert at(tape, "aiomysql.connection:Connection.commit") == []


def test_leaf_off_nests_the_driver_beneath_each_statement(mysql: Server) -> None:
    with (
        instrumentation(AiomysqlInstrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation, leaf=False),
        timeline() as tape,
    ):
        asyncio.run(workload(mysql))

    events = driver_events(tape)
    assert [event.data["statement"] for event in events] == DRIVER_STATEMENTS

    # Each workload statement's driver event nests beneath the
    # dialect's seam that issued it.

    for event in events:
        assert event.path == "aiomysql.cursors:Cursor.execute"
        parent = tape.parent_of(event)
        assert parent is not None and parent.path == EXECUTE

    (connect,) = at(tape, CONNECT)
    assert [child.path for child in tape.children_of(connect)] == [
        "aiomysql.connection:Connection._connect"
    ]

    (commit,) = at(tape, COMMIT)
    assert [child.path for child in tape.children_of(commit)] == [
        "aiomysql.connection:Connection.commit"
    ]


def test_raw_driver_use_beside_the_engine_records_at_top_level(
    mysql: Server,
) -> None:
    async def scenario() -> None:
        await workload(mysql)

        async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 2")
                await cursor.fetchone()

    with (
        instrumentation(AiomysqlInstrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation),
        timeline() as tape,
    ):
        asyncio.run(scenario())

    (raw,) = [event for event in tape.all if event.data.get("statement") == "SELECT 2"]
    assert raw.path == "aiomysql.cursors:Cursor.execute"
    assert tape.parent_of(raw) is None
