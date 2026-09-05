"""Instrumentation for mysqlclient, the driver imported as `MySQLdb`:
every query, the connections opened and the transaction boundaries
recorded as database events, by bindings on the Python classes the
driver's C core is wrapped in.

This module imports only wrapture. Everything that touches the driver
lives in the sibling modules, one per kind of seam (cursor.py for the
execute family, connection.py for connect and the connection's own
transaction boundaries), each importing only wrapture at top level
and reaching the driver through the module the hook is handed, so
loading this class when a config loads never imports MySQLdb ahead of
the hook meant to fire on its import.

The seams live in two submodules, and importing the `MySQLdb` package
loads neither of them: `MySQLdb.connections` (which imports
`MySQLdb.cursors`) is imported by the `connect` factory on the first
call. So there is one hook per submodule, each firing when its module
is imported, which for an application that only ever calls
`MySQLdb.connect()` is the moment before the first connection is
built. The entry point is `MySQLdb`, the name the driver is imported
and configured by, rather than `mysqlclient`, the name it is
installed by; wrapture resolves the installed version through the
import name.

PyMySQL can install itself under the `MySQLdb` name
(`pymysql.install_as_MySQLdb()`), for applications written against
the older driver, and a submodule imported through that name is then
PyMySQL's. Each hook checks the module it is handed is mysqlclient's
own and binds nothing otherwise: PyMySQL's classes are the pymysql
target's, and that target records them under their own names.
"""

from __future__ import annotations

from typing import Any

import wrapture
from wrapture import Setting

from . import connection, cursor


def is_mysqlclient(module: Any) -> bool:
    """Whether a module under the `MySQLdb` name is mysqlclient itself
    (the package, or its `connections` and `cursors` submodules), told
    by what only mysqlclient's own modules carry, rather than PyMySQL
    installed under that name."""

    name = getattr(module, "__name__", "")

    if name == "MySQLdb.cursors":
        return hasattr(module, "BaseCursor")

    # The package and its connections module both hold the C core.

    return hasattr(module, "_mysql")


class MySQLdbInstrumentation(wrapture.Instrumentation):
    """Query and transaction tracing for mysqlclient (MySQLdb)."""

    description = "Query and transaction tracing for mysqlclient (MySQLdb)."

    target = "MySQLdb"
    supports = ">=2.2.1,<3"
    removable = True

    settings = {
        "statement": Setting(
            False,
            "record the SQL text as handed to the driver on each query"
            " event; off by default because the driver cannot tell a"
            " literal an application interpolated from a placeholder,"
            " and the text is only safe to record when queries are"
            " parameterized",
        ),
    }

    @wrapture.instrumentation_hook("MySQLdb.cursors")
    def mysqldb_cursors(self, name: str, module: Any) -> None:
        """Bind the execute family once the cursor classes exist,
        unless the module is PyMySQL's under the alias."""

        if not is_mysqlclient(module):
            return

        cursor.instrument(module, self)

    @wrapture.instrumentation_hook("MySQLdb.connections")
    def mysqldb_connections(self, name: str, module: Any) -> None:
        """Bind the constructor and the transaction boundaries once the
        connection class exists, unless the module is PyMySQL's under
        the alias."""

        if not is_mysqlclient(module):
            return

        connection.instrument(module, self)
