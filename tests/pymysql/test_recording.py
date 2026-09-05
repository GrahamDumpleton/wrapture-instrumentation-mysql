"""What the instrumentation records: the connect in each of its
spellings, the execute family, stored procedures, the transaction
boundaries, the cursor subclasses, and what stays out of capture."""

from __future__ import annotations

import os
import sys
import warnings
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("pymysql")

import pymysql
import pymysql.cursors
from wrapture import Event, Tape, instrumentation, timeline

from tests.conftest import Server
from wrapture_instrumentation_mysql.pymysql import PymysqlInstrumentation

CONNECT = "pymysql.connections:Connection.connect"
EXECUTE = "pymysql.cursors:Cursor.execute"
EXECUTEMANY = "pymysql.cursors:Cursor.executemany"
CALLPROC = "pymysql.cursors:Cursor.callproc"


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

    connection = pymysql.connect(**mysql.kwargs)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE PROCEDURE {name}(IN x INT) SELECT x * 2")
        yield name
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP PROCEDURE IF EXISTS {name}")
        connection.close()


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def test_connect_records_a_database_leaf(mysql: Server, tape: Tape) -> None:
    pymysql.connect(**mysql.kwargs).close()

    (event,) = at(tape, CONNECT)
    contract(event, mysql, "CONNECT")
    assert event.label is None
    assert event.arguments is None
    assert event.result is None
    assert tape.children_of(event) == []


def test_every_spelling_of_connect_records_once(mysql: Server, tape: Tape) -> None:
    # pymysql.connect, Connect and Connection are one class, whose
    # constructor opens the socket through the bound method.

    pymysql.connect(**mysql.kwargs).close()
    pymysql.Connect(**mysql.kwargs).close()
    pymysql.Connection(**mysql.kwargs).close()

    events = at(tape, CONNECT)
    assert len(events) == 3
    assert len(tape.all) == 3


def test_connect_never_captures_its_arguments(mysql: Server, tape: Tape) -> None:
    pymysql.connect(**mysql.kwargs).close()

    # The constructor's arguments carry the password, and none of them
    # pass through the bound method, whose own argument is not
    # captured either.

    (event,) = at(tape, CONNECT)
    assert event.arguments is None
    assert "password" not in recorded(tape)


def test_a_refused_connection_records_its_error_and_never_the_password(
    mysql: Server, tape: Tape
) -> None:
    with pytest.raises(pymysql.err.OperationalError):
        pymysql.connect(**{**mysql.kwargs, "password": "wrong-hunter2"})

    (event,) = at(tape, CONNECT)
    contract(event, mysql, "CONNECT")
    assert isinstance(event.exception, pymysql.err.OperationalError)
    assert "wrong-hunter2" not in recorded(tape)


def test_a_deferred_connect_records_when_the_socket_opens(
    mysql: Server, tape: Tape
) -> None:
    connection = pymysql.connect(defer_connect=True, **mysql.kwargs)
    assert at(tape, CONNECT) == []

    connection.connect()
    connection.close()

    (event,) = at(tape, CONNECT)
    contract(event, mysql, "CONNECT")


def test_a_reconnecting_ping_records_a_connect(mysql: Server, tape: Tape) -> None:
    connection = pymysql.connect(**mysql.kwargs)
    connection.close()

    # PyMySQL 1.2 deprecates the reconnect argument but still honours
    # it; the warning is the driver's business.

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        connection.ping(reconnect=True)
    connection.close()

    assert len(at(tape, CONNECT)) == 2


def test_the_statements_connect_runs_itself_fold_into_the_connect(
    mysql: Server, tape: Tape
) -> None:
    # The driver runs SET NAMES, the sql_mode and the init_command
    # through a cursor of its own while connecting; all of it happens
    # beneath the CONNECT leaf.

    connection = pymysql.connect(
        sql_mode="TRADITIONAL", init_command="SET @wrapture = 1", **mysql.kwargs
    )
    connection.close()

    (event,) = tape.all
    assert event.path == CONNECT
    assert "statement" not in event.data


# ---------------------------------------------------------------------------
# the execute family
# ---------------------------------------------------------------------------


def test_execute_records_operation_but_no_statement(mysql: Server, tape: Tape) -> None:
    with pymysql.connect(**mysql.kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMPORARY TABLE secrets (value TEXT)")
            cursor.execute("INSERT INTO secrets VALUES (%s)", ("hunter2",))

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
    with (
        instrumentation(PymysqlInstrumentation, statement=True),
        timeline() as tape,
        pymysql.connect(**mysql.kwargs) as connection,
    ):
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMPORARY TABLE items (name TEXT)")
            cursor.execute("INSERT INTO items VALUES (%(name)s)", {"name": "widget"})
            cursor.execute("SELECT name FROM items WHERE name = %s", ("widget",))
            assert cursor.fetchall() == (("widget",),)

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
    with pymysql.connect(**mysql.kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMPORARY TABLE items (name TEXT)")
            cursor.executemany("INSERT INTO items VALUES (%s)", [("a",), ("b",)])
            cursor.executemany(
                "INSERT INTO items VALUES (%s)", (("g",) for _ in range(3))
            )
            cursor.execute("SELECT count(*) FROM items")
            assert cursor.fetchone() == (5,)

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

    with pymysql.connect(**mysql.kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMPORARY TABLE items (name TEXT, seen INT)")
            cursor.executemany("INSERT INTO items VALUES (%s, 0)", [("a",), ("b",)])
            cursor.executemany(
                "UPDATE items SET seen = 1 WHERE name = %s", [("a",), ("b",)]
            )

    (insert, update) = at(tape, EXECUTEMANY)
    contract(update, mysql, "UPDATE")
    assert update.result == 2
    assert len(at(tape, EXECUTE)) == 1


def test_the_statement_setting_records_the_executemany_template(
    mysql: Server,
) -> None:
    with (
        instrumentation(PymysqlInstrumentation, statement=True),
        timeline() as tape,
        pymysql.connect(**mysql.kwargs) as connection,
    ):
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMPORARY TABLE items (name TEXT)")
            cursor.executemany(
                "INSERT INTO items VALUES (%s)", [("first",), ("second",)]
            )

    (event,) = at(tape, EXECUTEMANY)
    assert event.data["statement"] == "INSERT INTO items VALUES (%s)"
    assert "first" not in recorded(tape)


def test_callproc_records_a_call_with_the_procedure(
    mysql: Server, procedure: str, tape: Tape
) -> None:
    with pymysql.connect(**mysql.kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.callproc(procedure, (21,))
            assert cursor.fetchone() == (42,)

    (event,) = at(tape, CALLPROC)
    contract(event, mysql, "CALL")
    assert event.data["procedure"] == procedure
    assert "statement" not in event.data

    # The procedure's name is captured as a name; its arguments reduce
    # to a count. The SET of the session variables and the CALL itself
    # go straight to the wire beneath the leaf, so nothing else is
    # recorded.

    assert event.arguments is not None
    assert event.arguments["procname"] == procedure
    assert event.arguments["args"] == "<1 values>"
    assert event.result == "<tuple>"
    assert [event.path for event in tape.all] == [CONNECT, CALLPROC]


def test_a_failing_query_records_its_exception(mysql: Server, tape: Tape) -> None:
    with pymysql.connect(**mysql.kwargs) as connection:
        with connection.cursor() as cursor:
            with pytest.raises(pymysql.err.ProgrammingError):
                cursor.execute("SELECT nope FROM nowhere")

    (event,) = at(tape, EXECUTE)
    contract(event, mysql, "SELECT")
    assert isinstance(event.exception, pymysql.err.ProgrammingError)


@pytest.mark.parametrize(
    "cursor_class",
    [
        pymysql.cursors.DictCursor,
        pymysql.cursors.SSCursor,
        pymysql.cursors.SSDictCursor,
    ],
    ids=lambda cls: str(cls.__name__),
)
def test_every_cursor_class_records_through_the_base_binding(
    mysql: Server, tape: Tape, cursor_class: type
) -> None:
    with pymysql.connect(**mysql.kwargs) as connection:
        with connection.cursor(cursor_class) as cursor:
            cursor.execute("SELECT %s AS token", ("s3cret-token",))
            rows = cursor.fetchall()

    assert list(rows) and "s3cret-token" in repr(rows)

    # The subclasses inherit execute, so the event's path names the
    # base class whichever ran; the value still never reaches the
    # record.

    (event,) = at(tape, EXECUTE)
    contract(event, mysql, "SELECT")
    assert "s3cret-token" not in recorded(tape)


def test_the_raw_query_path_is_not_recorded(mysql: Server, tape: Tape) -> None:
    connection = pymysql.connect(**mysql.kwargs)
    try:
        connection.query("SELECT 1")
        assert connection.affected_rows() == 1
    finally:
        connection.close()

    assert [event.path for event in tape.all] == [CONNECT]


# ---------------------------------------------------------------------------
# transaction boundaries
# ---------------------------------------------------------------------------


def test_begin_commit_and_rollback_record_their_operations(
    mysql: Server, tape: Tape
) -> None:
    with pymysql.connect(**mysql.kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMPORARY TABLE items (name TEXT)")

            connection.begin()
            cursor.execute("INSERT INTO items VALUES ('kept')")
            connection.commit()

            connection.begin()
            cursor.execute("INSERT INTO items VALUES ('dropped')")
            connection.rollback()

            cursor.execute("SELECT name FROM items")
            assert cursor.fetchall() == (("kept",),)

    (begin, _) = at(tape, "pymysql.connections:Connection.begin")
    contract(begin, mysql, "BEGIN")
    (commit,) = at(tape, "pymysql.connections:Connection.commit")
    contract(commit, mysql, "COMMIT")
    (rollback,) = at(tape, "pymysql.connections:Connection.rollback")
    contract(rollback, mysql, "ROLLBACK")

    for event in (begin, commit, rollback):
        assert event.arguments == {}
        assert event.result is None


def test_the_connection_context_manager_records_nothing_on_exit(
    mysql: Server, tape: Tape
) -> None:
    # Leaving the block closes the connection and performs no commit
    # or rollback, so there is no boundary to record.

    with pymysql.connect(**mysql.kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")

    assert not connection.open
    assert [event.path for event in tape.all] == [CONNECT, EXECUTE]


def test_autocommit_is_not_recorded(mysql: Server, tape: Tape) -> None:
    with pymysql.connect(**mysql.kwargs) as connection:
        connection.autocommit(True)
        assert connection.get_autocommit() is True

    assert [event.path for event in tape.all] == [CONNECT]


# ---------------------------------------------------------------------------
# the MySQLdb alias
# ---------------------------------------------------------------------------


def test_installed_as_mysqldb_the_driver_records_as_itself(
    mysql: Server, tape: Tape
) -> None:
    # install_as_MySQLdb() aliases the MySQLdb name to pymysql; an
    # application importing MySQLdb then runs PyMySQL's classes, which
    # record through the bindings already on them.

    if "MySQLdb" in sys.modules:
        pytest.skip("a real MySQLdb is imported in this process")

    pymysql.install_as_MySQLdb()
    try:
        import MySQLdb

        assert MySQLdb is pymysql

        with MySQLdb.connect(**mysql.kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)
    finally:
        sys.modules.pop("MySQLdb", None)

    assert [event.path for event in tape.all] == [CONNECT, EXECUTE]


# ---------------------------------------------------------------------------
# the shape of every event
# ---------------------------------------------------------------------------


def test_every_event_carries_the_contract_keys(
    mysql: Server, procedure: str, tape: Tape
) -> None:
    with pymysql.connect(**mysql.kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMPORARY TABLE items (name TEXT)")
            connection.begin()
            cursor.executemany("INSERT INTO items VALUES (%s)", [("a",), ("b",)])
            connection.commit()
            cursor.callproc(procedure, (1,))
            cursor.fetchall()
            connection.rollback()

    assert len(tape.all) == 7

    keys: set[str] = {"system", "operation", "database", "host", "port"}
    for event in tape.all:
        assert keys <= set(event.data), event.path
        assert event.category == "database", event.path

    data: dict[str, Any] = tape.all[0].data
    assert data["system"] == "mysql"

    # The database name is decoded from the bytes the driver keeps it
    # as once connected.

    assert all(isinstance(event.data["database"], str) for event in tape.all)
