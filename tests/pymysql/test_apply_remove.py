"""Applying and removing: the patched names on the driver's classes,
and that removal leaves them all as they were whatever the setting."""

from __future__ import annotations

import pytest

pytest.importorskip("pymysql")

import pymysql
import pymysql.connections
import pymysql.cursors
from wrapture import instrumentation, timeline

from tests.conftest import Server
from wrapture_instrumentation_mysql.pymysql import PymysqlInstrumentation


def choke_points() -> dict[str, object]:
    """The callables currently at every patched name."""

    return {
        "cursor_execute": pymysql.cursors.Cursor.execute,
        "cursor_executemany": pymysql.cursors.Cursor.executemany,
        "cursor_callproc": pymysql.cursors.Cursor.callproc,
        "connect": pymysql.connections.Connection.connect,
        "begin": pymysql.connections.Connection.begin,
        "commit": pymysql.connections.Connection.commit,
        "rollback": pymysql.connections.Connection.rollback,
    }


def untouched_points() -> dict[str, object]:
    """The callables at the names deliberately left alone, checked to
    stay so."""

    return {
        "module_connect": pymysql.connect,
        "exit": pymysql.connections.Connection.__exit__,
        "query": pymysql.connections.Connection.query,
        "autocommit": pymysql.connections.Connection.autocommit,
        "subclass_execute": pymysql.cursors.SSCursor.__dict__.get("execute"),
    }


@pytest.mark.parametrize("statement", [False, True])
def test_apply_then_remove_leaves_everything_as_it_was(statement: bool) -> None:
    # The statement setting shapes the recorded data, not the patch,
    # so the patched set is the same either way.

    before = choke_points()
    untouched = untouched_points()

    with instrumentation(PymysqlInstrumentation, statement=statement) as record:
        (instance,) = record.instrumentations

        assert instance.applied == ("pymysql",)

        current = choke_points()
        for name in before:
            assert current[name] is not before[name], name

        # pymysql.connect is the Connection class itself, so the class
        # object is untouched; the subclasses inherit the bound base
        # methods rather than gaining any of their own.

        assert untouched_points() == untouched

    current = choke_points()
    for name in before:
        assert current[name] is before[name], name

    assert not instance.applied


def test_after_removal_a_query_records_nothing(mysql: Server) -> None:
    with instrumentation(PymysqlInstrumentation):
        pass

    with timeline() as tape, pymysql.connect(**mysql.kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)

    assert tape.all == []


def test_a_connection_opened_while_applied_stops_recording_on_removal(
    mysql: Server,
) -> None:
    # The bindings sit on the classes, so a connection that outlives
    # the instrumentation keeps working and simply stops recording.

    with instrumentation(PymysqlInstrumentation):
        connection = pymysql.connect(**mysql.kwargs)

    try:
        with timeline() as tape:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)
            connection.commit()

        assert tape.all == []
    finally:
        connection.close()
