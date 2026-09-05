"""The connection seams: connections being opened, and the
transaction boundaries the connection itself performs, recorded as
database events around their awaits.

`aiomysql.connect(...)` is a plain function handing back an object
that can be awaited or entered with `async with`; the work happens in
a coroutine that builds the `Connection` and awaits its `_connect()`,
the method that opens the socket, shakes hands and authenticates. A
binding on the factory would record an instant, so `_connect` is the
seam, private though it is: one CONNECT event per socket opened,
spanning the await, whichever spelling the application used, and the
same seam serves a pool filling itself (its connections come through
`connect()` on the acquiring task) and the reconnect `ping()`
performs. It takes no arguments and none are captured; the server
keys are annotated from the instance beforehand (its host, port and
database are public properties the constructor has set), so a
refused connection still says where it was going. Once the handshake
is done, `_connect()` runs the `sql_mode` and `init_command`
statements through the connection's raw query and commits the
latter; those are inner calls of the CONNECT leaf and record nothing
of their own.

`begin()`, `commit()` and `rollback()` each send their statement as
a bare command rather than through a cursor, so they are bound in
their own right and record BEGIN, COMMIT and ROLLBACK around their
awaits. The connection's async context manager is not a transaction
boundary in aiomysql (its exit closes the connection and nothing
else), so it is not bound; `autocommit()` is a mode change and not
bound either.

`Connection.query()`, the raw path beneath the cursor's execute, is
not bound: the cursor is where the common path records.
"""

from __future__ import annotations

from typing import Any

import wrapture

from ..common import SYSTEM, captured, server_keys


def server_of(connection: Any) -> dict[str, Any]:
    """The `database`, `host` and `port` keys of an aiomysql
    connection, from its public properties."""

    return server_keys(
        getattr(connection, "host", None),
        getattr(connection, "port", None),
        getattr(connection, "db", None),
    )


def instrument(module: Any, instrumentation: wrapture.Instrumentation) -> None:
    """Bind the open, begin, commit and rollback on the connection
    class; register their removal as this trigger's cleanup."""

    connection_class = module.connection.Connection

    async def opens(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        wrapture.annotate(system=SYSTEM, operation="CONNECT", **server_of(instance))

        return await wrapped(*args, **kwargs)

    def performs(operation: str) -> Any:
        async def record(
            wrapped: Any,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            wrapture.annotate(system=SYSTEM, operation=operation, **server_of(instance))

            return await wrapped(*args, **kwargs)

        return record

    def database_binding(name: str, capture_args: Any = captured) -> wrapture.Binding:
        return wrapture.binding(
            connection_class,
            name,
            category="database",
            leaf=True,
            capture_args=capture_args,
            capture_result=captured,
        )

    named: dict[str, wrapture.Binding] = {}

    # The open: nothing captured from its arguments.

    connect = database_binding("_connect", "none")
    connect.on_call.decorates(opens)
    named["connect"] = connect

    # The transaction boundaries the connection performs itself.

    for method, operation in (
        ("begin", "BEGIN"),
        ("commit", "COMMIT"),
        ("rollback", "ROLLBACK"),
    ):
        bound = database_binding(method)
        bound.on_call.decorates(performs(operation))
        named[method] = bound

    group = wrapture.bindings(**named)
    group.apply()

    instrumentation.on_cleanup(group.remove)
