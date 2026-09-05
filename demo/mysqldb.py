"""Drive MySQLdb (mysqlclient) against a MySQL server with the
instrumentation applied.

The instrumentation is resolved by its entry point name and the
server is whatever WRAPTURE_MYSQL_URL names. The driver is built only
inside the docker test container, so `just demo-mysqldb` runs this
module there, against the compose file's own server. The calls cover
the shapes that matter: the connect, a table and inserts (one
executemany), a select through a DictCursor, a stored procedure, a
transaction with a rollback and a failing statement; then the same
with SQL text recording on. Each runs beneath an observed function,
so the leaves sit in a tree.

Two views of the run always print: the live stream and the tree
reconstructed with timings. With --otel the same events also export
as OpenTelemetry spans to an OTLP endpoint (OTEL_EXPORTER_OTLP_ENDPOINT,
which the container points at the host's localhost:4318), each a
CLIENT span carrying db.system.name, db.operation.name, db.namespace
and the server address.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any
from urllib.parse import unquote, urlsplit

import wrapture


def add_otel_sink() -> None:
    """Register the OpenTelemetry sink; exits with guidance when the
    optional dependencies are missing."""

    try:
        import wrapture.otel
    except ImportError as error:
        raise SystemExit(
            "the OpenTelemetry dependencies are not installed; run the"
            " demo through `just demo-mysqldb --otel`, which overlays"
            " wrapture[otel] for the run"
        ) from error

    wrapture.add_sink(wrapture.otel.sink(service_name="wrapture-mysqldb-demo"))


def connect_kwargs(url: str) -> dict[str, Any]:
    """The keyword arguments MySQLdb takes, from the URL form the
    environment variable uses."""

    parts = urlsplit(url)

    return {
        "host": parts.hostname or "localhost",
        "port": parts.port or 3306,
        "user": unquote(parts.username or "root"),
        "password": unquote(parts.password or ""),
        "database": parts.path.lstrip("/") or "wrapture",
    }


@wrapture.observed
def queries(url: str) -> None:
    """A table, some rows, a select through a DictCursor, a stored
    procedure, a transaction with a rollback and a failing statement."""

    import MySQLdb
    import MySQLdb.cursors

    procedure = f"wrapture_demo_double_{os.getpid()}"

    with MySQLdb.connect(**connect_kwargs(url)) as connection:
        cursor = connection.cursor()
        cursor.execute("CREATE TEMPORARY TABLE items (name TEXT)")
        cursor.execute("INSERT INTO items VALUES (%s)", ("widget",))
        cursor.executemany("INSERT INTO items VALUES (%s)", [("gadget",), ("gizmo",)])
        connection.commit()

        rows = connection.cursor(MySQLdb.cursors.DictCursor)
        rows.execute("SELECT name FROM items ORDER BY name")
        rows.fetchall()

        cursor.execute(f"CREATE PROCEDURE {procedure}(IN x INT) SELECT x * 2")
        try:
            cursor.callproc(procedure, (21,))
            cursor.fetchone()
        finally:
            cursor.execute(f"DROP PROCEDURE {procedure}")

        connection.begin()
        cursor.execute("INSERT INTO items VALUES ('undone')")
        connection.rollback()

        try:
            cursor.execute("SELECT nope FROM nowhere")
        except MySQLdb.ProgrammingError:
            pass


def main(arguments: list[str] | None = None) -> None:
    """Run the demo: apply the instrumentation, drive MySQLdb against
    the server, print the live stream and the tree, and flush any
    exporters."""

    parser = argparse.ArgumentParser(
        prog="demo.mysqldb",
        description="Drive MySQLdb against a MySQL server with the"
        " instrumentation applied, printing the live stream and the tree.",
    )
    parser.add_argument(
        "--otel",
        action="store_true",
        help="also export the events as OpenTelemetry spans over OTLP",
    )
    options = parser.parse_args(arguments)

    url = os.environ.get("WRAPTURE_MYSQL_URL")
    if not url:
        raise SystemExit(
            "WRAPTURE_MYSQL_URL is not set; `just demo-mysqldb` runs this"
            " demo inside docker against the compose file's server"
        )

    if options.otel:
        add_otel_sink()

    wrapture.add_sink(wrapture.Printer(stream=sys.stdout))

    print("== live stream ==")

    with wrapture.timeline() as tape:
        with wrapture.instrumentation("MySQLdb"):
            queries(url)

        with wrapture.instrumentation("MySQLdb", statement=True):
            queries(url)

    print()
    print("== tree ==")
    print(tape.tree(times=True))

    wrapture.shutdown()

    if options.otel:
        endpoint = os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"
        )
        print()
        print("== otel ==")
        print(f"spans flushed to {endpoint} as service wrapture-mysqldb-demo")


if __name__ == "__main__":
    main()
