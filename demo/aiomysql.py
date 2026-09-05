"""Drive aiomysql against a MySQL server with the instrumentation
applied.

The instrumentation is resolved by its entry point name and the
server is whatever WRAPTURE_MYSQL_URL names (`just mysql-start` runs
one and prints the URL). The calls cover the shapes that matter: the
connect, a table and inserts (one executemany), a select through a
DictCursor, a stored procedure, a transaction with a rollback, a
failing statement and a connection from a pool; then the same with
SQL text recording on. Each runs beneath an observed coroutine, so
the leaves sit in a tree.

Two views of the run always print: the live stream and the tree
reconstructed with timings. With --otel the same events also export
as OpenTelemetry spans to a local OTLP endpoint (http://localhost:4318
unless OTEL_EXPORTER_OTLP_ENDPOINT says otherwise), each a CLIENT
span carrying db.system.name, db.operation.name, db.namespace and
the server address.
"""

from __future__ import annotations

import argparse
import asyncio
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
            " demo through `just demo-aiomysql --otel`, which overlays"
            " wrapture[otel] for the run"
        ) from error

    wrapture.add_sink(wrapture.otel.sink(service_name="wrapture-aiomysql-demo"))


def connect_kwargs(url: str) -> dict[str, Any]:
    """The keyword arguments aiomysql takes, from the URL form the
    environment variable uses."""

    parts = urlsplit(url)

    return {
        "host": parts.hostname or "localhost",
        "port": parts.port or 3306,
        "user": unquote(parts.username or "root"),
        "password": unquote(parts.password or ""),
        "db": parts.path.lstrip("/") or "wrapture",
    }


@wrapture.observed
async def queries(url: str) -> None:
    """A table, some rows, a select through a DictCursor, a stored
    procedure, a transaction with a rollback, a failing statement and
    a query on a pooled connection."""

    import aiomysql

    procedure = f"wrapture_demo_double_{os.getpid()}"

    async with aiomysql.connect(**connect_kwargs(url)) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("CREATE TEMPORARY TABLE items (name TEXT)")
            await cursor.execute("INSERT INTO items VALUES (%s)", ("widget",))
            await cursor.executemany(
                "INSERT INTO items VALUES (%s)", [("gadget",), ("gizmo",)]
            )
            await connection.commit()

        async with connection.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute("SELECT name FROM items ORDER BY name")
            await cursor.fetchall()

        async with connection.cursor() as cursor:
            await cursor.execute(f"CREATE PROCEDURE {procedure}(IN x INT) SELECT x * 2")
            try:
                await cursor.callproc(procedure, (21,))
                await cursor.fetchone()
            finally:
                await cursor.execute(f"DROP PROCEDURE {procedure}")

            await connection.begin()
            await cursor.execute("INSERT INTO items VALUES ('undone')")
            await connection.rollback()

            try:
                await cursor.execute("SELECT nope FROM nowhere")
            except aiomysql.ProgrammingError:
                pass

    pool = await aiomysql.create_pool(minsize=1, maxsize=1, **connect_kwargs(url))
    try:
        async with pool.acquire() as pooled:
            async with pooled.cursor() as cursor:
                await cursor.execute("SELECT 1")
                await cursor.fetchone()
    finally:
        pool.close()
        await pool.wait_closed()


def main(arguments: list[str] | None = None) -> None:
    """Run the demo: apply the instrumentation, drive aiomysql against
    the server, print the live stream and the tree, and flush any
    exporters."""

    parser = argparse.ArgumentParser(
        prog="demo.aiomysql",
        description="Drive aiomysql against a MySQL server with the"
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
            "WRAPTURE_MYSQL_URL is not set; `just mysql-start` runs a server"
            " and prints the line to export"
        )

    if options.otel:
        add_otel_sink()

    wrapture.add_sink(wrapture.Printer(stream=sys.stdout))

    print("== live stream ==")

    with wrapture.timeline() as tape:
        with wrapture.instrumentation("aiomysql"):
            asyncio.run(queries(url))

        with wrapture.instrumentation("aiomysql", statement=True):
            asyncio.run(queries(url))

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
        print(f"spans flushed to {endpoint} as service wrapture-aiomysql-demo")


if __name__ == "__main__":
    main()
