# wrapture-instrumentation-mysql

Instrumentation for the MySQL client libraries, applied through
[wrapture](https://github.com/GrahamDumpleton/wrapture).

wrapture attaches bindings to arbitrary Python call sites without
modifying the code being observed, and its config layer can switch on
packaged instrumentation for a third-party package by name. This is
the MySQL package in that collection: one `wrapture.Instrumentation`
class per client library, so tracing every query, connection and
transaction your application sends to MySQL (or MariaDB, through the
same drivers) is one config entry and no code.

> **Status: beta, ahead of 1.0.0.** Developed against wrapture's
> beta series, with pre-releases published to
> [PyPI](https://pypi.org/project/wrapture-instrumentation-mysql/),
> and until 1.0.0 is final a plain `pip install
> wrapture-instrumentation-mysql` picks up the latest pre-release
> automatically, so there is no need to pin a specific version.

## Why a separate package

The core
[wrapture-instrumentation](https://github.com/GrahamDumpleton/wrapture-instrumentation)
package deliberately covers only the standard library and third-party
packages that can be exercised in-process, with no separate backend
product or service needed to test against. A MySQL driver is exactly
the kind of target the separate-package rule was drawn for: its tests
need a real server, so this package's suite runs one in a docker
container, and it carries the drivers as test dependencies (one of
them built from source against a MySQL client library) and its own
release cadence, so the core package's test matrix stays light. One
package covers every client library for the one backend.

## Installation

```console
$ pip install wrapture-instrumentation-mysql
```

Installing it brings wrapture and nothing else. No driver is a
dependency: each instrumentation is inert until its driver is present,
and wrapture checks the installed version against the range the
instrumentation supports at apply time.

## Using it

An `[[instrument]]` entry in `wrapture.toml` names the target:

```toml
[[instrument]]
name = "pymysql"

[[sink]]
type = "printer"
```

and the runner applies it before the application starts, so the patch
is in place before the driver is imported:

```console
$ python -m wrapture -m myapp
```

The same config works through
[autowrapt](https://github.com/GrahamDumpleton/autowrapt) injection
(`AUTOWRAPT_BOOTSTRAP=wrapture python myapp.py`); through
[manual setup](https://wrapture.readthedocs.io/en/latest/manual-setup.html),
a few lines in the application's own startup where wrapping the launch
from outside is awkward; and, in a test, through
`wrapture.instrumentation("pymysql")` scoping the instrumentation to
a block. The
[ad-hoc tracing guide](https://wrapture.readthedocs.io/en/latest/ad-hoc-tracing.html)
covers the config file itself.

To see what is installed, what it supports in the current
environment, and what settings it takes:

```console
$ python -m wrapture.tools instrumentation --verbose
```

## Provided instrumentation

| Target | Supported versions | Records | Settings |
| ------ | ------------------ | ------- | -------- |
| [`pymysql`](https://github.com/GrahamDumpleton/wrapture-instrumentation-mysql/blob/develop/src/wrapture_instrumentation_mysql/pymysql/README.md) | PyMySQL 1.1.1+ (1.x) | Every query as one `database` leaf, however it was issued (a cursor's `execute`, `executemany` or `callproc`, through whichever cursor class the application chose), plus the connection being opened and each transaction boundary the connection performs itself (`begin`, `commit`, `rollback`). Each event carries the system, the operation, and the database, host and port it reached; a failing statement records the driver's exception. The SQL text (the template with its placeholders) is recorded only with the `statement` setting on, bound parameters never. | `statement` |
| [`MySQLdb`](https://github.com/GrahamDumpleton/wrapture-instrumentation-mysql/blob/develop/src/wrapture_instrumentation_mysql/mysqldb/README.md) | mysqlclient 2.2.1+ (2.x), imported as `MySQLdb` | The same shapes through mysqlclient: every query as one `database` leaf (`execute`, `executemany`, `callproc`, through every cursor class), the connection being opened, and `begin`, `commit` and `rollback`, the last two bound over the C core's own methods. Each event carries the system, the operation, and the host and port it reached, plus the database from mysqlclient 2.2.7 on (earlier versions do not keep it); a failing statement records the driver's exception. The SQL text (the template with its placeholders) is recorded only with the `statement` setting on, bound parameters never. | `statement` |
| [`aiomysql`](https://github.com/GrahamDumpleton/wrapture-instrumentation-mysql/blob/develop/src/wrapture_instrumentation_mysql/aiomysql/README.md) | aiomysql 0.2+ (0.x) | The same shapes through aiomysql, each event recorded around its await: every query as one `database` leaf (`execute`, `executemany`, `callproc`, through every cursor class), the connection being opened (from a pool too), and `begin`, `commit` and `rollback`. Each event carries the system, the operation, and the database, host and port it reached; a failing statement records the driver's exception. The SQL text (the template with its placeholders) is recorded only with the `statement` setting on, bound parameters never. | `statement` |

The entry point name is the config's `name`, and is the import name
of the package the instrumentation patches (so mysqlclient's is
`MySQLdb`); the linked per-target README is the full user
documentation: what records, what the events carry, the setting, and
what is deliberately not traced.

## What is not traced

By design, and where it goes:

- Fetching rows: a query event closes when its execute returns, so
  time spent iterating rows afterwards is the application's. An
  unbuffered cursor (`SSCursor`) therefore records the send and the
  wait for the first packet, not the streaming of the rows.

- The connection's context manager: in these drivers leaving a
  `with connection:` block closes the connection and performs no
  commit or rollback, so it is not a transaction boundary and records
  nothing. The explicit `commit()` and `rollback()` calls do.

- Mode changes and housekeeping (`autocommit()`, `select_db()`,
  `ping()`, `set_character_set()`, `show_warnings()`): not database
  operations in the sense the events record.

- Pool bookkeeping (aiomysql): taking a connection from a pool and
  returning it are not database operations; the connections the pool
  opens do record.

- `LOAD DATA LOCAL INFILE` records as a statement like any other; the
  file's contents never do.

## Testing against a server

The test suite drives the real drivers against a real MySQL server.
`WRAPTURE_MYSQL_URL` in the environment names one
(`mysql://user:password@host:port/database`); without it the suite
runs a throwaway `mysql:8.4` container itself, which needs Docker
Desktop or another docker daemon. With neither it fails rather than
skips. The mysqlclient driver builds from source against a MySQL
client library, so its suite runs inside a docker container that
carries the toolchain (`just test-docker`), and a plain native run
skips that one suite visibly rather than asking for a client library
on the machine. [TESTING.md](TESTING.md) covers the details, including how
the suite copes with MySQL 8's `caching_sha2_password` authentication
(the readiness probe's first connection warms the server's credential
cache, after which every driver connects plainly; a real application
on a cold cache over a plain socket needs the driver's own RSA or TLS
option, which is the driver's concern, not the instrumentation's).

## Adding a target

Each client library is its own subpackage and entry point here. The
subpackage's `__init__.py` holds one `wrapture.Instrumentation`
subclass and imports only wrapture (and the package's own `common.py`,
which imports only wrapture too); everything that touches the driver
lives in sibling modules imported inside the hook. The class is
registered in `pyproject.toml` under
`[project.entry-points."wrapture.instrumentation"]`, and gets its own
test suite under `tests/<target>/` and a `README.md` linked from the
table above. The
[instrumentation packages](https://wrapture.readthedocs.io/en/latest/instrumentation-packages.html)
page of the wrapture documentation is the full contract; TESTING.md
here covers the tests and the server they run against.

## License

BSD 2-Clause. See
[LICENSE](https://github.com/GrahamDumpleton/wrapture-instrumentation-mysql/blob/develop/LICENSE).
