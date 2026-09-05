"""Applying and removing: the patched names on the driver's classes,
and that removal leaves them all as they were whatever the setting."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("aiomysql")

import aiomysql
import aiomysql.connection
import aiomysql.cursors
from wrapture import instrumentation, timeline

from tests.aiomysql.conftest import connect_kwargs
from tests.conftest import Server
from wrapture_instrumentation_mysql.aiomysql import AiomysqlInstrumentation

Connection = aiomysql.connection.Connection


def choke_points() -> dict[str, object]:
    """The callables currently at every patched name."""

    return {
        "cursor_execute": aiomysql.cursors.Cursor.execute,
        "cursor_executemany": aiomysql.cursors.Cursor.executemany,
        "cursor_callproc": aiomysql.cursors.Cursor.callproc,
        "connect": Connection._connect,
        "begin": Connection.begin,
        "commit": Connection.commit,
        "rollback": Connection.rollback,
    }


def untouched_points() -> dict[str, object]:
    """The callables at the names deliberately left alone, checked to
    stay so."""

    return {
        "module_connect": aiomysql.connect,
        "module_coroutine": aiomysql.connection._connect,
        "aexit": Connection.__aexit__,
        "query": Connection.query,
        "autocommit": Connection.autocommit,
        "subclass_execute": aiomysql.cursors.SSCursor.__dict__.get("execute"),
    }


@pytest.mark.parametrize("statement", [False, True])
def test_apply_then_remove_leaves_everything_as_it_was(statement: bool) -> None:
    # The statement setting shapes the recorded data, not the patch,
    # so the patched set is the same either way.

    before = choke_points()
    untouched = untouched_points()

    with instrumentation(AiomysqlInstrumentation, statement=statement) as record:
        (instance,) = record.instrumentations

        assert instance.applied == ("aiomysql",)

        current = choke_points()
        for name in before:
            assert current[name] is not before[name], name

        assert untouched_points() == untouched

    current = choke_points()
    for name in before:
        assert current[name] is before[name], name

    assert not instance.applied


async def select_one(mysql: Server) -> None:
    async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT 1")
            assert await cursor.fetchone() == (1,)
        await connection.commit()


def test_after_removal_a_query_records_nothing(mysql: Server) -> None:
    with instrumentation(AiomysqlInstrumentation):
        pass

    with timeline() as tape:
        asyncio.run(select_one(mysql))

    assert tape.all == []


def test_a_connection_opened_while_applied_stops_recording_on_removal(
    mysql: Server,
) -> None:
    # The bindings sit on the classes, so a connection that outlives
    # the instrumentation keeps working and simply stops recording.

    async def run() -> None:
        with instrumentation(AiomysqlInstrumentation):
            connection = await aiomysql.connect(**connect_kwargs(mysql))

        try:
            with timeline() as tape:
                async with connection.cursor() as cursor:
                    await cursor.execute("SELECT 1")
                    assert await cursor.fetchone() == (1,)
                await connection.commit()

            assert tape.all == []
        finally:
            connection.close()

    asyncio.run(run())
