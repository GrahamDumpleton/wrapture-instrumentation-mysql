"""What every MySQL driver's events have in common: the database
category's contract keys, the operation name derived from the SQL,
and the capture policy that keeps queries and their data out of the
record.

This module imports only wrapture, so a target subpackage can import
it at load time without dragging any driver in.

Every event carries `system` ("mysql") and `operation` (the SQL's
leading keyword, or CONNECT, BEGIN, COMMIT, ROLLBACK and CALL), plus
`database`, `host` and `port` read from the driver's connection, so
each event says which server it went to. The SQL text is recorded as
`statement` only when the target's `statement` setting is on, as the
application handed it to the driver and never with its bound
parameters, which no setting captures. The bindings sit on the
drivers' public methods, above their client-side parameter
interpolation, so a recorded statement is the template with its
placeholders.
"""

from __future__ import annotations

from typing import Any

SYSTEM = "mysql"

# The argument names under which the drivers take SQL text, and those
# under which they take its parameters; the capture policy reduces
# the first to a length and the second to a count.

_QUERY_NAMES = frozenset({"query", "operation", "sql", "statement"})
_PARAMETER_NAMES = frozenset(
    {"args", "params", "parameters", "seq_of_parameters", "vars"}
)


def operation_of(sql: str) -> str:
    """The SQL's leading keyword, uppercased: the low-cardinality
    operation name the database contract carries."""

    head = sql.split(None, 1)

    # A statement may end in its keyword ("COMMIT;"): the terminator is
    # not part of the operation.

    return head[0].upper().rstrip(";") if head else "?"


def statement_of(query: Any) -> str | None:
    """The SQL text of a query as the driver was handed it: a string
    as is, bytes decoded, anything else None."""

    if isinstance(query, str):
        return query

    if isinstance(query, (bytes, bytearray, memoryview)):
        try:
            return bytes(query).decode()
        except UnicodeDecodeError:
            return None

    return None


def server_keys(host: Any, port: Any, database: Any) -> dict[str, Any]:
    """The `database`, `host` and `port` keys from the values a
    driver's connection holds, normalised: bytes decoded (PyMySQL
    keeps the database name as bytes once connected), the port an
    int, and a value the driver does not know (None or empty) left
    out."""

    data: dict[str, Any] = {}

    for key, value in (("database", database), ("host", host), ("port", port)):
        if isinstance(value, (bytes, bytearray)):
            try:
                value = bytes(value).decode()
            except UnicodeDecodeError:
                continue

        if value in (None, ""):
            continue

        if key == "port":
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue

        data[key] = value

    return data


def statement_data(
    query: Any,
    server: dict[str, Any],
    record_statement: bool,
    operation: str | None = None,
) -> dict[str, Any]:
    """The data for one query event: the contract keys, the server
    keys given, and the statement text when the setting asks for it.
    The operation is the one given, else the query's leading keyword."""

    text = statement_of(query)

    data: dict[str, Any] = {"system": SYSTEM}

    if operation is not None:
        data["operation"] = operation
    elif text is not None:
        data["operation"] = operation_of(text)

    data.update(server)

    if record_statement and text is not None:
        data["statement"] = text

    return data


def captured(name: str | None, value: Any) -> Any:
    """SQL text reduces to its length, parameters to a count or their
    type (a parameter sequence may be a generator the driver has yet
    to consume, and is never iterated; none at all stays None), and
    every unnamed value to
    its type, except a number or None, which are kept (an execute
    returns the affected row count, a count and not data; the
    drivers' connect and boundary methods return nothing): the query
    and its data never reach the record through argument capture. A
    procedure name is a name, not data, and is kept."""

    if name in _QUERY_NAMES:
        text = statement_of(value)
        if text is not None:
            return f"<{len(text)} chars>"
        return f"<{type(value).__name__}>"

    if name in _PARAMETER_NAMES:
        if value is None:
            return None
        if isinstance(value, (list, tuple, dict)):
            return f"<{len(value)} values>"
        return f"<{type(value).__name__}>"

    if name is None:
        if value is None or isinstance(value, int):
            return value
        return f"<{type(value).__name__}>"

    return value
