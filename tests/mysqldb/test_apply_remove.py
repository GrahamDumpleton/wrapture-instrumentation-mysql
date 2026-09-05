"""Applying and removing: the patched names on the driver's classes,
the C-inherited boundaries shadowed and then restored exactly, and
that removal leaves everything as it was whatever the setting."""

from __future__ import annotations

import pytest

pytest.importorskip("MySQLdb")

import MySQLdb
import MySQLdb._mysql
import MySQLdb.connections
import MySQLdb.cursors
from wrapture import instrumentation, timeline

from tests.conftest import Server
from wrapture_instrumentation_mysql.mysqldb import MySQLdbInstrumentation

Connection = MySQLdb.connections.Connection


def choke_points() -> dict[str, object]:
    """The callables currently at every patched name, as resolved
    through the class."""

    return {
        "cursor_execute": MySQLdb.cursors.BaseCursor.execute,
        "cursor_executemany": MySQLdb.cursors.BaseCursor.executemany,
        "cursor_callproc": MySQLdb.cursors.BaseCursor.callproc,
        "init": Connection.__init__,
        "begin": Connection.begin,
        "commit": Connection.commit,
        "rollback": Connection.rollback,
    }


def untouched_points() -> dict[str, object]:
    """The callables at the names deliberately left alone, checked to
    stay so: the factory functions, the connection's own housekeeping,
    the C type's own methods, and the concrete cursor classes' own
    namespaces."""

    return {
        "connect": MySQLdb.connect,
        "Connect": MySQLdb.Connect,
        "Connection": MySQLdb.Connection,
        "exit": Connection.__exit__,
        "query": Connection.query,
        "autocommit": Connection.autocommit,
        "c_commit": MySQLdb._mysql.connection.commit,
        "c_rollback": MySQLdb._mysql.connection.rollback,
        "cursor_execute": MySQLdb.cursors.Cursor.__dict__.get("execute"),
        "dict_cursor_execute": MySQLdb.cursors.DictCursor.__dict__.get("execute"),
    }


@pytest.mark.parametrize("statement", [False, True])
def test_apply_then_remove_leaves_everything_as_it_was(statement: bool) -> None:
    # The statement setting shapes the recorded data, not the patch,
    # so the patched set is the same either way.

    before = choke_points()
    untouched = untouched_points()

    with instrumentation(MySQLdbInstrumentation, statement=statement) as record:
        (instance,) = record.instrumentations

        assert instance.applied == ("MySQLdb.cursors", "MySQLdb.connections")

        current = choke_points()
        for name in before:
            assert current[name] is not before[name], name

        assert untouched_points() == untouched

    current = choke_points()
    for name in before:
        assert current[name] is before[name], name

    assert not instance.applied


def test_the_inherited_boundaries_are_shadowed_then_deleted() -> None:
    # commit and rollback are the C type's, inherited: the binding
    # puts a wrapper in the Python class's own namespace, and removal
    # deletes it rather than leaving a copy of the C method behind, so
    # the class is exactly as it was.

    assert "commit" not in vars(Connection)
    assert "rollback" not in vars(Connection)

    with instrumentation(MySQLdbInstrumentation):
        assert "commit" in vars(Connection)
        assert "rollback" in vars(Connection)

    assert "commit" not in vars(Connection)
    assert "rollback" not in vars(Connection)
    assert Connection.commit is MySQLdb._mysql.connection.commit
    assert Connection.rollback is MySQLdb._mysql.connection.rollback


def test_after_removal_a_query_records_nothing(mysql: Server) -> None:
    with instrumentation(MySQLdbInstrumentation):
        pass

    with timeline() as tape, MySQLdb.connect(**mysql.kwargs) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        assert cursor.fetchone() == (1,)
        connection.commit()

    assert tape.all == []


def test_a_connection_opened_while_applied_stops_recording_on_removal(
    mysql: Server,
) -> None:
    # The bindings sit on the classes, so a connection that outlives
    # the instrumentation keeps working and simply stops recording;
    # its commit resolves to the C method again.

    with instrumentation(MySQLdbInstrumentation):
        connection = MySQLdb.connect(**mysql.kwargs)

    try:
        with timeline() as tape:
            cursor = connection.cursor()
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)
            connection.commit()
            connection.rollback()

        assert tape.all == []
    finally:
        connection.close()
