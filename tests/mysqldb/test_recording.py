"""What the instrumentation records: the connect in each of its
spellings, the execute family, stored procedures, the transaction
boundaries through the C-inherited methods, the cursor classes, the
server keys per driver version, and what stays out of capture."""

from __future__ import annotations

import os
import sys
import warnings
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("MySQLdb")

import MySQLdb
import MySQLdb.connections
import MySQLdb.cursors
from wrapture import Event, Tape, instrumentation, timeline

from tests.conftest import Server
from wrapture_instrumentation_mysql.mysqldb import MySQLdbInstrumentation

CONNECT = "MySQLdb.connections:Connection.__init__"
EXECUTE = "MySQLdb.cursors:BaseCursor.execute"
EXECUTEMANY = "MySQLdb.cursors:BaseCursor.executemany"
CALLPROC = "MySQLdb.cursors:BaseCursor.callproc"

# The host and database attributes the server keys come from arrived
# in mysqlclient 2.2.7; before that the host comes from the C core's
# host info and the database is not knowable.

HAS_ATTRIBUTES = MySQLdb.version_info[:3] >= (2, 2, 7)


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
    assert event.data["host"] == mysql.host
    assert event.data["port"] == mysql.port

    if HAS_ATTRIBUTES:
        assert event.data["database"] == mysql.database
    else:
        assert "database" not in event.data


@pytest.fixture
def procedure(mysql: Server) -> Iterator[str]:
    """A stored procedure with a name of its own (procedures cannot be
    temporary), doubling its one argument; dropped afterwards.

    Its own connection and CREATE would record on a tape already
    running, so a test takes this fixture ahead of `tape` in its
    argument list, which is the order pytest sets them up in.
    """

    name = f"wrapture_double_{os.getpid()}"

    connection = MySQLdb.connect(**mysql.kwargs)
    try:
        cursor = connection.cursor()
        cursor.execute(f"CREATE PROCEDURE {name}(IN x INT) SELECT x * 2")
        yield name
    finally:
        cursor = connection.cursor()
        cursor.execute(f"DROP PROCEDURE IF EXISTS {name}")
        connection.close()


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def test_connect_records_a_database_leaf(mysql: Server, tape: Tape) -> None:
    MySQLdb.connect(**mysql.kwargs).close()

    (event,) = at(tape, CONNECT)
    contract(event, mysql, "CONNECT")
    assert event.label is None
    assert event.arguments is None
    assert event.result is None
    assert tape.children_of(event) == []


def test_every_spelling_of_connect_records_once(mysql: Server, tape: Tape) -> None:
    # connect, Connect and Connection are one factory function, and a
    # direct construction goes through the same constructor.

    MySQLdb.connect(**mysql.kwargs).close()
    MySQLdb.Connect(**mysql.kwargs).close()
    MySQLdb.Connection(**mysql.kwargs).close()
    MySQLdb.connections.Connection(**mysql.kwargs).close()

    events = at(tape, CONNECT)
    assert len(events) == 4
    assert len(tape.all) == 4


def test_connect_never_captures_its_arguments(mysql: Server, tape: Tape) -> None:
    # The constructor's arguments carry the password, in either of the
    # driver's spellings.

    MySQLdb.connect(**mysql.kwargs).close()

    deprecated = dict(mysql.kwargs)
    deprecated["passwd"] = deprecated.pop("password")
    deprecated["db"] = deprecated.pop("database")
    MySQLdb.connect(**deprecated).close()

    events = at(tape, CONNECT)
    assert len(events) == 2
    assert all(event.arguments is None for event in events)
    assert "password" not in recorded(tape)
    assert "passwd" not in recorded(tape)


def test_a_refused_connection_records_its_error_and_never_the_password(
    mysql: Server, tape: Tape
) -> None:
    with pytest.raises(MySQLdb.OperationalError):
        MySQLdb.connect(**{**mysql.kwargs, "password": "wrong-hunter2"})

    (event,) = at(tape, CONNECT)
    assert event.data["operation"] == "CONNECT"
    assert isinstance(event.exception, MySQLdb.OperationalError)
    assert "wrong-hunter2" not in recorded(tape)

    # Where this version sets the host attribute before connecting,
    # the event still says where it was going.

    if HAS_ATTRIBUTES:
        assert event.data["host"] == mysql.host
        assert event.data["database"] == mysql.database


def test_the_statements_connect_runs_itself_fold_into_the_connect(
    mysql: Server, tape: Tape
) -> None:
    # The driver sets the character set, the sql_mode and runs the
    # init_command as part of connecting; nothing of it records apart
    # from the CONNECT.

    connection = MySQLdb.connect(
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
    with MySQLdb.connect(**mysql.kwargs) as connection:
        cursor = connection.cursor()
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
        instrumentation(MySQLdbInstrumentation, statement=True),
        timeline() as tape,
        MySQLdb.connect(**mysql.kwargs) as connection,
    ):
        cursor = connection.cursor()
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
    with MySQLdb.connect(**mysql.kwargs) as connection:
        cursor = connection.cursor()
        cursor.execute("CREATE TEMPORARY TABLE items (name TEXT)")
        cursor.executemany("INSERT INTO items VALUES (%s)", [("a",), ("b",)])
        cursor.executemany("INSERT INTO items VALUES (%s)", (("g",) for _ in range(3)))
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

    with MySQLdb.connect(**mysql.kwargs) as connection:
        cursor = connection.cursor()
        cursor.execute("CREATE TEMPORARY TABLE items (name TEXT, seen INT)")
        cursor.executemany("INSERT INTO items VALUES (%s, 0)", [("a",), ("b",)])
        cursor.executemany(
            "UPDATE items SET seen = 1 WHERE name = %s", [("a",), ("b",)]
        )

    (insert, update) = at(tape, EXECUTEMANY)
    contract(update, mysql, "UPDATE")
    assert update.result == 2
    assert len(at(tape, EXECUTE)) == 1


def test_callproc_records_a_call_with_the_procedure(
    mysql: Server, procedure: str, tape: Tape
) -> None:
    with MySQLdb.connect(**mysql.kwargs) as connection:
        cursor = connection.cursor()
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
    with MySQLdb.connect(**mysql.kwargs) as connection:
        cursor = connection.cursor()
        with pytest.raises(MySQLdb.ProgrammingError):
            cursor.execute("SELECT nope FROM nowhere")

    (event,) = at(tape, EXECUTE)
    contract(event, mysql, "SELECT")
    assert isinstance(event.exception, MySQLdb.ProgrammingError)


@pytest.mark.parametrize(
    "cursor_class",
    [
        MySQLdb.cursors.Cursor,
        MySQLdb.cursors.DictCursor,
        MySQLdb.cursors.SSCursor,
        MySQLdb.cursors.SSDictCursor,
    ],
    ids=lambda cls: str(cls.__name__),
)
def test_every_cursor_class_records_through_the_base_binding(
    mysql: Server, tape: Tape, cursor_class: type
) -> None:
    with MySQLdb.connect(**mysql.kwargs) as connection:
        cursor = connection.cursor(cursor_class)
        cursor.execute("SELECT %s AS token", ("s3cret-token",))
        rows = cursor.fetchall()
        cursor.close()

    assert list(rows) and "s3cret-token" in repr(rows)

    # Each class composes BaseCursor with its mixins rather than
    # descending from Cursor, so the binding on the base is the one
    # every class records through, and the value never reaches the
    # record.

    (event,) = at(tape, EXECUTE)
    contract(event, mysql, "SELECT")
    assert "s3cret-token" not in recorded(tape)


def test_the_raw_query_path_is_not_recorded(mysql: Server, tape: Tape) -> None:
    connection = MySQLdb.connect(**mysql.kwargs)
    try:
        connection.query(b"SELECT 1")
        connection.store_result()
    finally:
        connection.close()

    assert [event.path for event in tape.all] == [CONNECT]


# ---------------------------------------------------------------------------
# transaction boundaries
# ---------------------------------------------------------------------------


def test_begin_commit_and_rollback_record_their_operations(
    mysql: Server, tape: Tape
) -> None:
    with MySQLdb.connect(**mysql.kwargs) as connection:
        cursor = connection.cursor()
        cursor.execute("CREATE TEMPORARY TABLE items (name TEXT)")

        connection.begin()
        cursor.execute("INSERT INTO items VALUES ('kept')")
        connection.commit()

        connection.begin()
        cursor.execute("INSERT INTO items VALUES ('dropped')")
        connection.rollback()

        cursor.execute("SELECT name FROM items")
        assert cursor.fetchall() == (("kept",),)

    (begin, _) = at(tape, "MySQLdb.connections:Connection.begin")
    contract(begin, mysql, "BEGIN")
    (commit,) = at(tape, "MySQLdb.connections:Connection.commit")
    contract(commit, mysql, "COMMIT")
    (rollback,) = at(tape, "MySQLdb.connections:Connection.rollback")
    contract(rollback, mysql, "ROLLBACK")

    # The C methods take no arguments and return nothing; whether the
    # capture records an empty mapping or nothing at all depends on
    # whether this build of the C method exposes a signature.

    for event in (commit, rollback):
        assert not event.arguments
        assert event.result is None


def test_the_connection_context_manager_records_nothing_on_exit(
    mysql: Server, tape: Tape
) -> None:
    # Leaving the block closes the connection and performs no commit
    # or rollback, so there is no boundary to record.

    with MySQLdb.connect(**mysql.kwargs) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")

    assert not connection.open
    assert [event.path for event in tape.all] == [CONNECT, EXECUTE]


def test_autocommit_is_not_recorded(mysql: Server, tape: Tape) -> None:
    with MySQLdb.connect(**mysql.kwargs) as connection:
        connection.autocommit(True)
        assert connection.get_autocommit() is True

    assert [event.path for event in tape.all] == [CONNECT]


# ---------------------------------------------------------------------------
# the server keys per version
# ---------------------------------------------------------------------------


def test_the_server_keys_follow_what_this_version_records(
    mysql: Server, tape: Tape
) -> None:
    # From 2.2.7 the connection keeps the host and database it was
    # opened with; before that the host is read back from the client
    # library's host description and the database is unknown. The
    # port is a member of the C type on every version.

    with MySQLdb.connect(**mysql.kwargs) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")

        assert connection.get_host_info().startswith(f"{mysql.host} via ")

    (event,) = at(tape, EXECUTE)
    assert event.data["host"] == mysql.host
    assert event.data["port"] == mysql.port

    if HAS_ATTRIBUTES:
        assert event.data["database"] == mysql.database
    else:
        assert "database" not in event.data


# ---------------------------------------------------------------------------
# beside PyMySQL
# ---------------------------------------------------------------------------


def test_pymysql_installed_as_mysqldb_records_through_its_own_target(
    mysql: Server,
) -> None:
    pymysql = pytest.importorskip("pymysql")

    from wrapture_instrumentation_mysql.mysqldb import is_mysqlclient
    from wrapture_instrumentation_mysql.pymysql import PymysqlInstrumentation

    assert is_mysqlclient(MySQLdb)
    assert not is_mysqlclient(pymysql)

    # With both targets applied and PyMySQL aliased under the MySQLdb
    # name, a connection through that name runs PyMySQL's classes and
    # records once, under PyMySQL's own paths, with nothing from this
    # target and no error.

    real = sys.modules["MySQLdb"]

    with (
        instrumentation(MySQLdbInstrumentation),
        instrumentation(PymysqlInstrumentation),
        timeline() as tape,
    ):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            pymysql.install_as_MySQLdb()

        try:
            import MySQLdb as aliased

            assert aliased is pymysql

            with aliased.connect(**mysql.kwargs) as connection:
                cursor = connection.cursor()
                cursor.execute("SELECT 1")
                assert cursor.fetchone() == (1,)
        finally:
            sys.modules["MySQLdb"] = real

    assert [event.path for event in tape.all] == [
        "pymysql.connections:Connection.connect",
        "pymysql.cursors:Cursor.execute",
    ]


# ---------------------------------------------------------------------------
# the shape of every event
# ---------------------------------------------------------------------------


def test_every_event_carries_the_contract_keys(
    mysql: Server, procedure: str, tape: Tape
) -> None:
    with MySQLdb.connect(**mysql.kwargs) as connection:
        cursor = connection.cursor()
        cursor.execute("CREATE TEMPORARY TABLE items (name TEXT)")
        connection.begin()
        cursor.executemany("INSERT INTO items VALUES (%s)", [("a",), ("b",)])
        connection.commit()
        cursor.callproc(procedure, (1,))
        cursor.fetchall()

        # A procedure call leaves an extra, empty result set behind
        # its own, which the driver requires the caller to step past
        # before the connection takes another command.

        while cursor.nextset():
            pass

        connection.rollback()

    assert len(tape.all) == 7

    keys: set[str] = {"system", "operation", "host", "port"}
    if HAS_ATTRIBUTES:
        keys.add("database")

    for event in tape.all:
        assert keys <= set(event.data), event.path
        assert event.category == "database", event.path

    data: dict[str, Any] = tape.all[0].data
    assert data["system"] == "mysql"
