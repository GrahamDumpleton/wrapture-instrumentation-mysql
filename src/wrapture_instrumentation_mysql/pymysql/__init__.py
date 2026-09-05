"""Instrumentation for PyMySQL: every query, the connections opened
and the transaction boundaries recorded as database events, by
bindings on the pure Python classes the driver is made of.

This module imports only wrapture. Everything that touches PyMySQL
lives in the sibling modules, one per kind of seam (cursor.py for the
execute family, connection.py for connect and the connection's own
transaction boundaries), each importing only wrapture at top level
and reaching PyMySQL through the package the hook is handed, so
loading this class when a config loads never imports PyMySQL ahead of
the hook meant to fire on its import.

One trigger suffices: importing pymysql initialises the two
submodules the seams live in (`pymysql.connections` and
`pymysql.cursors`), so by the time the hook fires all the classes
exist under the package.
"""

from __future__ import annotations

from typing import Any

import wrapture
from wrapture import Setting

from . import connection, cursor


class PymysqlInstrumentation(wrapture.Instrumentation):
    """Query and transaction tracing for PyMySQL."""

    description = "Query and transaction tracing for PyMySQL."

    target = "pymysql"
    supports = ">=1.1.1,<2"
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

    @wrapture.instrumentation_hook("pymysql")
    def pymysql(self, name: str, module: Any) -> None:
        """Bind the cursor and connection seams once pymysql exists."""

        cursor.instrument(module, self)
        connection.instrument(module, self)
