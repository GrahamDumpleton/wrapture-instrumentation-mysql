# MySQLdb instrumentation

Query and transaction tracing for
[mysqlclient](https://mysqlclient.readthedocs.io/), the driver
imported as `MySQLdb`, a C extension over the MySQL client library.
Entry point name `MySQLdb`, the package it patches (the name the
driver is imported and configured by, not `mysqlclient`, the name it
is installed by); supports mysqlclient 2.2.1 and later, below 3;
fully removable. Every cursor class the driver offers is covered, and
so is MariaDB behind the same driver (the events still say `mysql`:
the driver cannot tell, and the wire protocol is the same).

## Enabling it

An `[[instrument]]` entry in `wrapture.toml` (with at least one sink
to hear the events):

```toml
[[instrument]]
name = "MySQLdb"

[[sink]]
type = "printer"
```

run under wrapture's runner (`python -m wrapture -m myapp`), or in a
test through the context manager:

```python
with wrapture.instrumentation("MySQLdb"):
    ...
```

## What you see

One `database` leaf per operation: the connection being opened, each
query however it was issued (a cursor's `execute`, `executemany` or
`callproc`), and each transaction boundary the connection performs
itself (`begin`, `commit`, `rollback`):

```
MySQLdb.connections:Connection.__init__()
MySQLdb.cursors:BaseCursor.execute(query='<33 chars>', args='<1 values>')  -> 1
MySQLdb.cursors:BaseCursor.executemany(query='<33 chars>', args='<2 values>')  -> 2
MySQLdb.cursors:BaseCursor.callproc(procname='double_it', args='<1 values>')  -> '<tuple>'
MySQLdb.connections:Connection.commit()
```

- Only the driver's core is C; its connection and cursor classes are
  Python, so the instrumentation binds their methods in place:
  `execute`, `executemany` and `callproc` on
  `MySQLdb.cursors.BaseCursor`, the one class that defines them and
  that `Cursor`, `DictCursor`, `SSCursor` and `SSDictCursor` all
  build on, so every cursor class records through the one binding
  and the event's path names the base class whichever ran; and the
  constructor, `begin`, `commit` and `rollback` on
  `MySQLdb.connections.Connection`.

- `MySQLdb.connect(...)` (and its `Connect` and `Connection`
  spellings) is a factory for `connections.Connection`, whose
  constructor opens the connection, which is why the seam is the
  constructor: one `CONNECT` event per connection, however it was
  spelt. The `SET NAMES`, `sql_mode` and `init_command` statements
  the driver runs as part of connecting happen beneath the `CONNECT`
  leaf and do not record separately.

- `commit()` and `rollback()` are methods of the C core, inherited by
  the Python class. The instrumentation binds them by name on the
  Python class, which shadows the inherited method for as long as it
  is applied and leaves the C type untouched; removal deletes the
  shadow and the name resolves to the C method again.

- Every event carries the database contract keys `system` (`mysql`)
  and `operation` (the SQL's leading keyword, or `CONNECT`, `BEGIN`,
  `COMMIT`, `ROLLBACK`, `CALL`), which wrapture's OpenTelemetry
  export maps to `db.system.name` and `db.operation.name`, plus the
  `host` and `port` the connection reached and, from mysqlclient
  2.2.7 on, the `database` it was opened with (earlier versions keep
  no record of it on the connection, so their events carry no
  `database`, and their `host` comes from the client library's own
  host description). The database is the one named at connect time:
  a later `select_db()` changes the server's idea of the current
  database but not the driver's attribute, and so not the recorded
  one.

- A `callproc` records `CALL` with the procedure's name as
  `procedure`. The driver sets the arguments as session variables
  with a `SET` statement before the `CALL`; that round trip happens
  beneath the leaf and is not a separate event.

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
  event captures none of its arguments, which carry the password.
  The bindings sit above the driver's client-side interpolation of
  `%s` and `%(name)s` placeholders, so the recorded text is the
  template with its placeholders rather than the statement with its
  values. There is no obfuscation at this layer, because rewriting
  SQL to strip literals is a losing game outside a real lexer:
  record the text where queries are parameterized, leave it off
  where they are not.

- A failing statement records the driver's exception
  (`MySQLdb.ProgrammingError` for an unknown table, say) on the event
  as it escapes, and a refused connection records its
  `OperationalError` on the `CONNECT` event. Fetching is not
  recorded: a query event closes when its execute returns, so time
  spent iterating rows afterwards is not attributed to the database,
  and an unbuffered cursor (`SSCursor`, `SSDictCursor`) records the
  send and the wait for the first packet rather than the streaming
  of the rows.

- If PyMySQL has been installed under the `MySQLdb` name
  (`pymysql.install_as_MySQLdb()`), this instrumentation binds
  nothing, since the classes behind that name are PyMySQL's; the
  `pymysql` target records them, under their own names.

## Not recorded

- The connection's context manager: leaving a `with connection:`
  block closes the connection in mysqlclient and performs no commit
  or rollback, so its exit is not a transaction boundary and records
  nothing (the explicit `commit()` and `rollback()` do, and so does
  `begin()`).

- `autocommit()`, a mode change, and the housekeeping calls
  `select_db()`, `set_character_set()`, `set_sql_mode()`, `ping()`,
  `show_warnings()` and `kill()`.

- `Connection.query()`, the raw path beneath the cursor's `execute`
  that applications may call directly: the cursor is where the
  common path records.

- `mogrify()`, which builds a statement but sends nothing.

## Settings

| Setting | Default | Controls |
| ------- | ------- | -------- |
| `statement` | `false` | Whether each query event records the SQL text as handed to the driver, as `statement`: the template with its `%s` or `%(name)s` placeholders, since the bindings sit above the driver's interpolation. Off by default because the driver cannot tell a literal an application interpolated from a placeholder; turn it on when your queries are parameterized, the text then carrying placeholders rather than data. Bound parameters are never recorded either way. |

```toml
[[instrument]]
name = "MySQLdb"
statement = true
```

## With the sqlalchemy instrumentation

An instrumented mysqlclient beneath the core package's `sqlalchemy`
target composes through that target's `leaf` setting. With the
default `leaf = true` each statement is one event and the driver's
own events stay out of the tree; with `leaf = false` the driver's
events nest beneath each statement, `BaseCursor.execute` under the
dialect's `do_execute`, `BaseCursor.executemany` under the MySQL
dialect's own `do_executemany`, the driver's `CONNECT` under the
dialect's `connect`, and `commit` under the engine's commit. Raw
MySQLdb use beside the engine records at the top level either way. A
little of the dialect's own housekeeping also shows up regardless,
because it runs straight against the driver outside the recorded
seams: the version and `sql_mode` queries the MySQL dialect makes
when it opens a connection, and the pool's reset-on-return rollback.

## How it patches

For the implementation detail see the module docstrings of
[cursor.py](cursor.py) (the execute family) and
[connection.py](connection.py) (the constructor, the C-inherited
boundaries and where the server keys come from per version).
