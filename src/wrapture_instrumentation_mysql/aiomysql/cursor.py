"""The cursor seams: the execute family on the cursor class every
cursor descends from, recorded as database events around their
awaits.

aiomysql's cursors are pure Python, so each coroutine method is bound
in place on `aiomysql.cursors.Cursor`. `execute`, `executemany` and
`callproc` are the doors every query passes through, whichever cursor
class the application chose: `DictCursor`, `SSCursor`, `SSDictCursor`
and the deserialising cursors all inherit the three unchanged (the
unbuffered cursor overrides only the internals beneath them), and so
does the cursor class SQLAlchemy's async adapter derives, so one
binding each on `Cursor` covers every cursor class, and the events
say `Cursor.execute` whichever subclass ran.

Each binding is a leaf, which is what keeps one application call to
one event: `executemany` runs its batches through `execute` (once per
multi-row INSERT it assembles, or once per parameter set for any
other statement), and `callproc` sets each argument as a session
variable with a SET before the CALL; those inner round trips happen
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
    class; register their removal as this trigger's cleanup."""

    cursor_class = module.cursors.Cursor

    settings = instrumentation.settings
    record_statement = bool(settings["statement"])

    def data_for(
        cursor: Any, query: Any, operation: str | None = None
    ) -> dict[str, Any]:
        return statement_data(
            query, server_of(cursor.connection), record_statement, operation
        )

    async def queries(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        query = args[0] if args else kwargs.get("query")
        wrapture.annotate(**data_for(instance, query))

        return await wrapped(*args, **kwargs)

    async def calls(
        wrapped: Any, instance: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        procname = args[0] if args else kwargs.get("procname")

        data = data_for(instance, None, "CALL")
        if isinstance(procname, str):
            data["procedure"] = procname
        wrapture.annotate(**data)

        return await wrapped(*args, **kwargs)

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
