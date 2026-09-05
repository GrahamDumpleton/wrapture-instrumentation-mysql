# aiomysql instrumentation

Query and transaction tracing for
[aiomysql](https://aiomysql.readthedocs.io/), the asyncio MySQL
driver built on PyMySQL's protocol code. Entry point name `aiomysql`,
the package it patches; supports aiomysql 0.2 and later, below 1;
fully removable. Every cursor class the driver offers is covered,
connections from a pool included, and so is MariaDB behind the same
driver (the events still say `mysql`: the driver cannot tell, and
the wire protocol is the same).

## Enabling it

An `[[instrument]]` entry in `wrapture.toml` (with at least one sink
to hear the events):

```toml
[[instrument]]
name = "aiomysql"

[[sink]]
type = "printer"
```

run under wrapture's runner (`python -m wrapture -m myapp`), or in a
test through the context manager:

```python
with wrapture.instrumentation("aiomysql"):
    ...
```

## What you see

One `database` leaf per operation, recorded around its await: the
connection being opened, each query however it was issued (a cursor's
`execute`, `executemany` or `callproc`), and each transaction
boundary the connection performs itself (`begin`, `commit`,
`rollback`):

```
aiomysql.connection:Connection._connect()
aiomysql.cursors:Cursor.execute(query='<33 chars>', args='<1 values>')  -> 1
aiomysql.cursors:Cursor.executemany(query='<33 chars>', args='<2 values>')  -> 2
aiomysql.cursors:Cursor.callproc(procname='double_it', args='<1 values>')  -> '<tuple>'
aiomysql.connection:Connection.commit()
```

- aiomysql's classes are pure Python, so the instrumentation binds
  their coroutine methods in place: `Cursor.execute`, `executemany`
  and `callproc` on `aiomysql.cursors.Cursor`, which `DictCursor`,
  `SSCursor`, `SSDictCursor` and the deserialising cursors all
  inherit (as does the cursor class SQLAlchemy's async adapter
  derives), so every cursor class records through the one binding
  and the event's path names the base class whichever ran; and
  `Connection._connect`, `begin`, `commit` and `rollback` on
  `aiomysql.connection.Connection`. Each event spans the await.

- `aiomysql.connect(...)` is a factory whose work happens in the
  connection's own `_connect()` coroutine, the method that opens the
  socket and authenticates; that is the seam, private though it is,
  because the factory itself returns in an instant: one `CONNECT`
  event per socket opened, whether the application awaited
  `connect()`, entered it with `async with`, took a connection from
  a pool (which fills itself through the same factory, on the task
  that asked), or had `ping()` reconnect. The `sql_mode` and
  `init_command` statements the driver runs as part of connecting,
  and the commit after the latter, happen beneath the `CONNECT`
  leaf and do not record separately.

- Every event carries the database contract keys `system` (`mysql`)
  and `operation` (the SQL's leading keyword, or `CONNECT`, `BEGIN`,
  `COMMIT`, `ROLLBACK`, `CALL`), which wrapture's OpenTelemetry
  export maps to `db.system.name` and `db.operation.name`, plus the
  `database`, `host` and `port` the connection was opened with, from
  its public properties, so every span says which server it went
  to. The database is the one named at connect time: a later
  `select_db()` changes the server's idea of the current database
  but not the driver's property, and so not the recorded one.

- A `callproc` records `CALL` with the procedure's name as
  `procedure`. The driver sets each argument as a session variable
  with a `SET` statement before the `CALL`; those round trips happen
  beneath the leaf and are not separate events.

- An `executemany` is one event: the driver assembles a multi-row
  `INSERT` (or `REPLACE`) from the template and sends it in batches,
  and runs any other statement once per parameter set, all of it
  through its own `execute` beneath the `executemany` leaf, so the
  inner calls record nothing of their own. With the `statement`
  setting on the event carries the template as handed over, never
  the multi-row statement the driver built.

- The capture policy is deliberate about sensitive data: bound
  parameters are never recorded, under any setting (they reduce to a
  count, or to a type name for a parameter sequence the driver has
  yet to consume, which is never iterated); the SQL text reduces to
  its length unless the `statement` setting is on; and the connect
  event captures no arguments (the factory's, which carry the
  password, never pass through the seam at all). The bindings sit
  above the driver's client-side interpolation of `%s` and
  `%(name)s` placeholders, so the recorded text is the template with
  its placeholders rather than the statement with its values. There
  is no obfuscation at this layer, because rewriting SQL to strip
  literals is a losing game outside a real lexer: record the text
  where queries are parameterized, leave it off where they are not.

- A failing statement records the driver's exception
  (`aiomysql.ProgrammingError`, which is PyMySQL's, for an unknown
  table, say) on the event as it escapes, and a refused connection
  records its `OperationalError` on the `CONNECT` event with the
  server keys it was going to. Fetching is not recorded: a query
  event closes when its execute returns, so time spent iterating
  rows afterwards is not attributed to the database, and an
  unbuffered cursor (`SSCursor`, `SSDictCursor`) records the send and
  the wait for the first packet rather than the streaming of the
  rows.

## Not recorded

- The connection's async context manager: leaving an `async with`
  block closes the connection in aiomysql and performs no commit or
  rollback, so its exit is not a transaction boundary and records
  nothing (the explicit `commit()` and `rollback()` do, and so does
  `begin()`).

- Pool bookkeeping: taking a connection from a pool and returning it
  are not database operations. The connections the pool opens do
  record, as `CONNECT`.

- `autocommit()`, a mode change (it sends `SET AUTOCOMMIT`), and the
  housekeeping calls `select_db()`, `set_charset()`, `ping()` (except
  for the reconnect it may perform), `show_warnings()`,
  `ensure_closed()` and `kill()`.

- `Connection.query()`, the raw path beneath the cursor's `execute`:
  the cursor is where the common path records.

- `mogrify()`, which builds a statement but sends nothing.

## Settings

| Setting | Default | Controls |
| ------- | ------- | -------- |
| `statement` | `false` | Whether each query event records the SQL text as handed to the driver, as `statement`: the template with its `%s` or `%(name)s` placeholders, since the bindings sit above the driver's interpolation. Off by default because the driver cannot tell a literal an application interpolated from a placeholder; turn it on when your queries are parameterized, the text then carrying placeholders rather than data. Bound parameters are never recorded either way. |

```toml
[[instrument]]
name = "aiomysql"
statement = true
```

## With the sqlalchemy instrumentation

An instrumented aiomysql beneath the core package's `sqlalchemy`
target, through the async engine (`mysql+aiomysql://`), composes
through that target's `leaf` setting. With the default `leaf = true`
each statement is one event and the driver's own events stay out of
the tree; with `leaf = false` the driver's events nest beneath each
statement, `Cursor.execute` under the dialect's `do_execute`, the
driver's `CONNECT` under the dialect's `connect`, and `commit` under
the engine's commit. Raw aiomysql use beside the engine records at
the top level either way. A little of the dialect's own housekeeping
also shows up regardless, because it runs straight against the
driver outside the recorded seams: the version and `sql_mode`
queries the MySQL dialect makes when it opens a connection, and the
pool's reset-on-return rollback.

## How it patches

For the implementation detail see the module docstrings of
[cursor.py](cursor.py) (the execute family) and
[connection.py](connection.py) (the open and the connection's own
boundaries).
