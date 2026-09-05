"""The cursor seams: the execute family on the class every cursor
descends from, recorded as database events.

mysqlclient's cursors are pure Python over the C result type.
`execute`, `executemany` and `callproc` live on
`MySQLdb.cursors.BaseCursor`; the four public classes (`Cursor`,
`DictCursor`, `SSCursor`, `SSDictCursor`) are compositions of
`BaseCursor` with mixins that define only the fetching and result
handling, so `BaseCursor` is the one owner of the three methods and
the binding must go there: bound by the name `Cursor.execute` the
wrapper would land in `Cursor`'s own namespace, and `DictCursor` does
not descend from `Cursor`. One binding each on `BaseCursor` covers
every cursor class, and the events say `BaseCursor.execute` whichever
subclass ran.

Each binding is a leaf, which is what keeps one application call to
one event: `executemany` runs its batches through `execute` (once per
multi-row INSERT it assembles, or once per parameter set for any
other statement), and `callproc` sets its arguments as session
variables with a SET before the CALL; those inner round trips happen
beneath the leaf and record nothing of their own. `execute` sits
above the driver's client-side interpolation, so the SQL text the
event can carry is the template with its placeholders, and
`executemany` records its template, never the multi-row statement
the driver assembles from it. Bound parameters are never recorded.

Fetching rows is not recorded: a query event closes when its execute
returns, the model every database target here follows, so an
unbuffered cursor's event spans the send and the wait for the first
packet, not the streaming of the rows.
"""

from __future__ import annotations

from typing import Any

import wrapture

from ..common import captured, statement_data
from .connection import server_of


def instrument(module: Any, instrumentation: wrapture.Instrumentation) -> None:
    """Bind execute, executemany and callproc on the base cursor
    class of the cursors module; register their removal as this
    trigger's cleanup."""

    cursor_class = module.BaseCursor

    settings = instrumentation.settings
    record_statement = bool(settings["statement"])

    def data_for(
        cursor: Any, query: Any, operation: str | None = None
    ) -> dict[str, Any]:
        return statement_data(
            query, server_of(cursor.connection), record_statement, operation
        )

    def queries(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        query = args[0] if args else kwargs.get("query")
        wrapture.annotate(**data_for(instance, query))

        return wrapped(*args, **kwargs)

    def calls(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        procname = args[0] if args else kwargs.get("procname")

        # The driver takes the name as str or bytes; the event carries
        # it as text.

        if isinstance(procname, (bytes, bytearray)):
            try:
                procname = bytes(procname).decode()
            except UnicodeDecodeError:
                procname = None

        data = data_for(instance, None, "CALL")
        if isinstance(procname, str):
            data["procedure"] = procname
        wrapture.annotate(**data)

        return wrapped(*args, **kwargs)

    named: dict[str, wrapture.Binding] = {}

    for method, decorator in (
        ("execute", queries),
        ("executemany", queries),
        ("callproc", calls),
    ):
        bound = wrapture.binding(
            cursor_class,
            method,
            category="database",
            leaf=True,
            capture_args=captured,
            capture_result=captured,
        )
        bound.on_call.decorates(decorator)
        named[method] = bound

    group = wrapture.bindings(**named)
    group.apply()

    instrumentation.on_cleanup(group.remove)
