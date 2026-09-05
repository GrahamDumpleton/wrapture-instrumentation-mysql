"""The connection seams: connections being opened, and the
transaction boundaries the connection itself performs, recorded as
database events.

`pymysql.connect` is not a function but the `Connection` class under
another name (`connect = Connect = Connection`), and its constructor
opens the socket by calling `Connection.connect()` unless
`defer_connect` was asked for, in which case the application calls
that method itself; `ping(reconnect=True)` calls it again to
reconnect. So `Connection.connect` is the one door every connection
passes through, and it is the seam bound: one CONNECT event per
socket opened, whichever spelling the application used. Its only
parameter is a pre-made socket, and nothing is captured from it; the
server keys are annotated from the instance, whose host, port and
database the constructor has set before it connects, so a refused
connection still says where it was going. Once the handshake is
done, `connect()` runs `SET NAMES`, the `sql_mode` and `init_command`
statements through a cursor of its own; those are inner calls of the
CONNECT leaf and record nothing of their own.

`begin()`, `commit()` and `rollback()` each send their statement as
a bare command rather than through a cursor, so they are bound in
their own right and record BEGIN, COMMIT and ROLLBACK. The
connection's context manager is not a transaction boundary in PyMySQL
(its exit closes the connection and nothing else), so it is not
bound; `autocommit()` is a mode change and not bound either.

`Connection.query()`, the raw path beneath the cursor's execute, is
documented for applications that want it directly and is not bound:
the cursor is where the common path records, and a second event
shape for the rare direct use is not worth it.
"""

from __future__ import annotations

from typing import Any

import wrapture

from ..common import SYSTEM, captured, server_keys


def server_of(connection: Any) -> dict[str, Any]:
    """The `database`, `host` and `port` keys of a PyMySQL connection,
    from the attributes its constructor sets (`db` becomes bytes once
    connected; the common normaliser decodes it)."""

    return server_keys(
        getattr(connection, "host", None),
        getattr(connection, "port", None),
        getattr(connection, "db", None),
    )


def instrument(module: Any, instrumentation: wrapture.Instrumentation) -> None:
    """Bind connect, begin, commit and rollback on the connection
    class; register their removal as this trigger's cleanup."""

    connection_class = module.connections.Connection

    def opens(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        wrapture.annotate(system=SYSTEM, operation="CONNECT", **server_of(instance))

        return wrapped(*args, **kwargs)

    def performs(operation: str) -> Any:
        def record(
            wrapped: Any,
            instance: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
        ) -> Any:
            wrapture.annotate(system=SYSTEM, operation=operation, **server_of(instance))

            return wrapped(*args, **kwargs)

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

    connect = database_binding("connect", "none")
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
