"""With the core package's sqlalchemy instrumentation applied
alongside, over the mysqldb dialect: the default leaf keeps the
driver's events out of the tree, leaf off nests them beneath each
statement (an executemany beneath the MySQL dialect's own
do_executemany), and raw driver use beside the engine still
records."""

from __future__ import annotations

import pytest

pytest.importorskip("MySQLdb")
pytest.importorskip("sqlalchemy")
pytest.importorskip("wrapture_instrumentation")

import MySQLdb
from sqlalchemy import create_engine, text
from wrapture import Event, Tape, instrumentation, timeline
from wrapture_instrumentation.database.sqlalchemy import SQLAlchemyInstrumentation

from tests.conftest import Server
from wrapture_instrumentation_mysql.mysqldb import MySQLdbInstrumentation

EXECUTE = "sqlalchemy.engine.default:DefaultDialect.do_execute"
EXECUTEMANY = "sqlalchemy.dialects.mysql.mysqldb:MySQLDialect_mysqldb.do_executemany"
CONNECT = "sqlalchemy.engine.default:DefaultDialect.connect"
COMMIT = "sqlalchemy.engine.base:Connection._commit_impl"

# The statements as SQLAlchemy is handed them, and as the driver
# then sees them: the mysqldb dialect compiles a named parameter to a
# positional placeholder (its paramstyle is `format`, where the
# pymysql dialect's is `pyformat`).

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
    """The MySQLdb events that belong to the workload, leaving out the
    dialect's own setup queries and the pool's reset-on-return
    rollback, which run outside SQLAlchemy's seams."""

    return [
        event
        for event in tape.all
        if event.path.startswith("MySQLdb.")
        and event.data.get("statement") in DRIVER_STATEMENTS
    ]


def engine_url(mysql: Server) -> str:
    return mysql.url.replace("mysql://", "mysql+mysqldb://")


def workload(mysql: Server) -> None:
    engine = create_engine(engine_url(mysql))

    with engine.begin() as connection:
        connection.execute(text(WORKLOAD[0][0]))
        connection.execute(text(WORKLOAD[1][0]), [{"name": "a"}, {"name": "b"}])
        connection.execute(text(WORKLOAD[2][0])).fetchall()

    engine.dispose()


def test_the_default_leaf_keeps_the_driver_out(mysql: Server) -> None:
    with (
        instrumentation(MySQLdbInstrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation),
        timeline() as tape,
    ):
        workload(mysql)

    # The dialect's seams record the workload (and the dialect's own
    # setup queries beside it, which are do_execute calls too); the
    # executemany goes through the MySQL dialect's own override.

    assert len(at(tape, EXECUTE)) >= 2
    assert len(at(tape, EXECUTEMANY)) == 1
    assert len(at(tape, CONNECT)) == 1
    assert len(at(tape, COMMIT)) == 1

    # Every workload statement ran beneath a sqlalchemy leaf, so the
    # driver's own events for them stay out of the tape.

    assert driver_events(tape) == []
    assert at(tape, "MySQLdb.connections:Connection.__init__") == []
    assert at(tape, "MySQLdb.connections:Connection.commit") == []


def test_leaf_off_nests_the_driver_beneath_each_statement(mysql: Server) -> None:
    with (
        instrumentation(MySQLdbInstrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation, leaf=False),
        timeline() as tape,
    ):
        workload(mysql)

    events = driver_events(tape)
    assert [event.data["statement"] for event in events] == DRIVER_STATEMENTS

    # Each workload statement's driver event nests beneath the
    # dialect's seam that issued it.

    create, insert, select = events
    for event in (create, select):
        assert event.path == "MySQLdb.cursors:BaseCursor.execute"
        parent = tape.parent_of(event)
        assert parent is not None and parent.path == EXECUTE

    assert insert.path == "MySQLdb.cursors:BaseCursor.executemany"
    parent = tape.parent_of(insert)
    assert parent is not None and parent.path == EXECUTEMANY
    assert insert.arguments is not None
    assert insert.arguments["args"] == "<2 values>"

    (connect,) = at(tape, CONNECT)
    assert [child.path for child in tape.children_of(connect)] == [
        "MySQLdb.connections:Connection.__init__"
    ]

    (commit,) = at(tape, COMMIT)
    assert [child.path for child in tape.children_of(commit)] == [
        "MySQLdb.connections:Connection.commit"
    ]


def test_raw_driver_use_beside_the_engine_records_at_top_level(
    mysql: Server,
) -> None:
    with (
        instrumentation(MySQLdbInstrumentation, statement=True),
        instrumentation(SQLAlchemyInstrumentation),
        timeline() as tape,
    ):
        workload(mysql)

        with MySQLdb.connect(**mysql.kwargs) as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT 2")
            cursor.fetchone()

    (raw,) = [event for event in tape.all if event.data.get("statement") == "SELECT 2"]
    assert raw.path == "MySQLdb.cursors:BaseCursor.execute"
    assert tape.parent_of(raw) is None
