"""The connection seams: connections being opened, and the
transaction boundaries the connection itself performs, recorded as
database events.

mysqlclient's `Connection` is a Python subclass of the C type
`_mysql.connection`, and everything bound here is bound on that
Python class. `MySQLdb.connect` (also `Connect` and `Connection`,
three names for one factory function) returns
`connections.Connection(...)`, whose `__init__` normalises the
arguments and calls the C initialiser, which opens the connection;
so `Connection.__init__` is the one door every spelling and a direct
construction go through, and it is the seam bound: one CONNECT event
per connection. Its arguments carry the password and none are
captured; the server keys are annotated from the instance once the
call is over, and on a failure from whatever the instance already
knows.

`commit()` and `rollback()` do not exist on the Python class: they
are methods of the C type, inherited. Binding them by name on the
Python class installs the wrapper in its own namespace, shadowing the
inherited slot, and the C type is untouched; removal deletes the
shadow, since wrapture restores an owner to exactly its prior own
namespace, and the name resolves to the C method again. `begin()` is
Python (it sends BEGIN through the connection's raw query) and is
bound the same way. The connection's context manager is not a
transaction boundary in mysqlclient (its exit closes the connection
and nothing else), so it is not bound; `autocommit()` is a mode
change and not bound either.

`Connection.query()`, the raw path beneath the cursor's execute, is
documented for applications that want it directly and is not bound:
the cursor is where the common path records.

The server keys come from the `host` and `db` attributes the
constructor sets from mysqlclient 2.2.7 on, and the `port` member the
C type has always had. Before 2.2.7 there are no such attributes, and
on any version `host` is None when the driver was left to pick the
local socket; the C core's `get_host_info()` then says where the
connection went ("127.0.0.1 via TCP/IP", "Localhost via UNIX
socket"), and the database name is not knowable, so events on those
versions carry no `database`. The host info is never asked of a
connection whose open failed: the core marks its handle open before
the attempt, so its own guard does not catch that case, and on the
older versions reading the host description of a handle that never
connected takes the interpreter down.
"""

from __future__ import annotations

from typing import Any

import wrapture

from ..common import SYSTEM, captured, server_keys


def server_of(connection: Any, connected: bool = True) -> dict[str, Any]:
    """The `database`, `host` and `port` keys of a mysqlclient
    connection: from its attributes where this version sets them,
    else from the C core's host info, which is asked only of a
    connection that did connect."""

    host = getattr(connection, "host", None)
    database = getattr(connection, "db", None)

    if host is None and connected:
        try:
            info = connection.get_host_info()
        except Exception:
            info = None

        if isinstance(info, str) and info:
            host = info.split(" via ", 1)[0]
            if host == "Localhost":
                host = "localhost"

    # The port member reads 0 until the C initialiser has run.

    try:
        port = connection.port
    except Exception:
        port = None
    if port == 0:
        port = None

    return server_keys(host, port, database)


def instrument(module: Any, instrumentation: wrapture.Instrumentation) -> None:
    """Bind the constructor, begin, commit and rollback on the
    connection class of the connections module; register their
    removal as this trigger's cleanup."""

    connection_class = module.Connection

    def opens(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        wrapture.annotate(system=SYSTEM, operation="CONNECT")

        # A failed open still says where it was going, from whatever
        # this version's constructor set on the instance beforehand.

        try:
            result = wrapped(*args, **kwargs)
        except BaseException:
            wrapture.annotate(**server_of(instance, connected=False))
            raise

        wrapture.annotate(**server_of(instance))

        return result

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

    connect = database_binding("__init__", "none")
    connect.on_call.decorates(opens)
    named["connect"] = connect

    # The transaction boundaries the connection performs itself; two
    # of them inherited from the C type and shadowed on the Python
    # class for the duration.

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
