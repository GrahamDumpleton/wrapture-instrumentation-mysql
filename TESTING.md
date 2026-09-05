# Testing

## Where the tests are

Tests live in the [tests/](tests/) directory at the top of the
repository, separate from the package code in
src/wrapture_instrumentation_mysql/. Test files are named `test_*.py`
and are discovered by pytest, configured via the
`[tool.pytest.ini_options]` section of [pyproject.toml](pyproject.toml).

The directory has two levels:

- Package-level tests directly under tests/: the version, the rule
  that importing the package or loading any registered class never
  imports a target, and the listing tool reporting every entry
  cleanly.

- One subdirectory per target, `tests/<target>/` (`tests/pymysql/`,
  `tests/mysqldb/`), holding that instrumentation's suite: settings validation, applying
  and removing the class directly, the whole path through
  `wrapture.instrumentation()` with a timeline recording what the
  bindings observe, resolving the entry point by name, a check that
  the installed driver satisfies the class's `supports` range, and
  the composition tests with the core package's sqlalchemy target.

Shared helpers and the server fixture live in
[tests/conftest.py](tests/conftest.py).

## The MySQL server

The suites drive the real drivers against a real MySQL server;
nothing is mocked. The session-scoped `mysql` fixture supplies it, in
one of two ways:

- `WRAPTURE_MYSQL_URL` in the environment names a server already
  running (`mysql://user:password@host:port/database`), and the
  fixture uses it and starts nothing. This is how CI's service
  container, the compose file's `mysql` service and a server of your
  own are used. Name the host as `127.0.0.1` rather than `localhost`:
  to the MySQL client library, and so to mysqlclient, `localhost`
  means the unix socket, and a server in a container publishes only
  its TCP port (PyMySQL always uses TCP and does not care).

- Otherwise the fixture runs a throwaway container from the official
  `mysql:8.4` image on a random localhost port, waits until a real
  connection from the host answers, and removes the container at the
  end of the session. This needs the docker CLI and a running daemon
  (Docker Desktop, or anything docker-compatible). The server takes
  about ten seconds to come up.

With neither, the session fails with a message saying so rather than
skipping, so a green run always means the suites ran. The fixture
yields a `Server` with the parts in both spellings the drivers take
(`server.url`, `server.kwargs`). Tests isolate themselves with
temporary tables (`CREATE TEMPORARY TABLE`), private to their
connection and gone when it closes, so no cleanup fixtures are
needed; what cannot be temporary (a stored procedure) gets a unique
name and is dropped in a finally.

An interrupted run can leave its throwaway container behind;
`just mysql-clean` removes any such container.

### Authentication

MySQL 8.4 creates its users with `caching_sha2_password`, and the
first connection a user makes over a plain (non-TLS) socket needs the
server's RSA public key to send the password safely; after that the
server caches the credential and later connections take a fast path
until it restarts. The fixture's readiness probe is a real PyMySQL
connection as root, which does the first exchange (PyMySQL 1.2
negotiates TLS by default, and its `rsa` extra in the test group
covers the 1.1 lines), so every later connection by any driver in
the session takes the fast path and no driver needs TLS or the key.
That is a property of the server, not a trick, and it holds on CI's
service container the same way.

## Running the tests

All tooling in this project goes through
[uv](https://docs.astral.sh/uv/), which manages the project
environment and installs the package, its development dependencies
(including pytest) and the drivers the tests need. Nothing MySQL is
installed on the machine: the server runs in docker, PyMySQL is pure
Python, and mysqlclient, which builds from source against a MySQL
client library, is built only inside the docker test container and on
CI (see below).

The simplest way to run the test suite is via the Justfile target,
which runs natively on the default Python version and starts a
throwaway server unless `WRAPTURE_MYSQL_URL` is set:

```console
just test
```

This runs every suite but the MySQLdb one, which skips itself
(visibly, in pytest's summary) because its driver is not in the
native environment; `just test-docker` below runs it. Should you
have a MySQL client library and pkg-config on the machine anyway,
`uv sync --group mysqlclient` builds the driver into the native
environment and the suite then runs natively too.

Extra arguments are passed through to pytest, for example:

```console
just test -v
just test tests/pymysql/test_recording.py
just test -k transaction
```

One target's suite alone:

```console
just test-target pymysql
```

Equivalently, run pytest directly with uv:

```console
uv run pytest
```

To run against a longer-lived server instead of a throwaway one per
session, start the compose file's server and export the URL it
prints:

```console
just mysql-start
export WRAPTURE_MYSQL_URL=mysql://root:mysql@127.0.0.1:33069/wrapture
just test
just mysql-stop
```

## Watching what the tests record

The suites assert on tapes rather than printing anything, but the
recorded events can be watched live for visual verification. Setting
WRAPTURE_PRINTER in the environment installs a process-wide
wrapture.Printer sink for the session, streaming one line to stderr as
each operation begins and a closing line with its outcome and timing;
pytest captures stderr, so add -s to see it:

```console
WRAPTURE_PRINTER=1 just test tests/pymysql -s
```

The same events can go to an OpenTelemetry backend instead, for
checking the spans, their kinds and their attributes in something
like otel-desktop-viewer. Setting WRAPTURE_OTEL installs wrapture's
OpenTelemetry sink for the session (exporting over OTLP to
http://localhost:4318, or wherever OTEL_EXPORTER_OTLP_ENDPOINT
points) and flushes it when the session ends; the `test-otel` recipe
sets the variable and overlays the wrapture[otel] dependencies the
sink needs:

```console
just test-otel tests/pymysql/test_recording.py -k transaction
```

Each event the tests record arrives as its own single-span trace:
the tests drive the driver directly with nothing of their own around
the calls, and a `timeline()` roots what it records (an enclosing
block or observed function outside the timeline is not the parent of
what happens inside it), so a root span per test would have to be
opened inside each test's own timeline and would then appear on the
tape the assertions read.

For a purpose-built run rather than the tests' traffic, the demo
module under demo/ applies the instrumentation, drives the driver
against the server `WRAPTURE_MYSQL_URL` names, and prints both the
live stream and the reconstructed tree with timings:

```console
just mysql-start
export WRAPTURE_MYSQL_URL=mysql://root:mysql@127.0.0.1:33069/wrapture
just demo-pymysql
```

With --otel the same events also export as OpenTelemetry spans over
OTLP (to http://localhost:4318, or wherever
OTEL_EXPORTER_OTLP_ENDPOINT points), for verifying the spans in a
local backend such as Jaeger; the Justfile target overlays the
wrapture[otel] dependencies for the run:

```console
just demo-pymysql --otel
```

The MySQLdb demo runs inside the docker test container, where its
driver is built, against the compose file's own server (which it
starts if need be); with --otel it exports to the host's
localhost:4318 through host.docker.internal:

```console
just demo-mysqldb
just demo-mysqldb --otel
```

## Testing across Python versions

The project supports Python 3.12 through 3.15. Free threaded builds
are not in the matrix: the instrumentation is pure Python and behaves
the same on them, and a free threaded row would only be testing
whether the drivers' own C extensions build without the GIL, which is
their concern. The supported list is defined at the top of the
[Justfile](Justfile); the default version used by plain `just test`
is pinned in [.python-version](.python-version).

The full matrix runs inside docker. The image in the
[Dockerfile](Dockerfile) carries the toolchain the mysqlclient driver
needs to build from source (it ships no Linux or macOS wheels), so
the machine never needs a MySQL client library. The
[compose.yml](compose.yml) file defines the server and a `tests`
container built from that image; uv fetches the requested interpreter
inside the container and installs the `test` and `mysqlclient`
dependency groups into a per-version environment, both kept on named
volumes so reruns pay for neither (the driver builds once per
version, on the first run).

```console
just test-all
just test-docker 3.15
```

The first run per version downloads the interpreter and the wheels.
The suite can also run natively in a per-version environment
(.venv-VERSION) that leaves the default .venv untouched, without the
MySQLdb suite:

```console
just test-python 3.13
```

## Testing across driver versions

The dependency groups install each driver at whatever version the
lock resolves. The instrumentation's `supports` range is kept honest
by running its suite against other lines of the driver. Each line
has a place in `pymysql_versions` or `mysqlclient_versions` in the
Justfile, run one at a time by `just test-pymysql 1.1.1` or
`just test-mysqldb 2.2.1`, or all by the `-all` forms, and the CI
workflow runs the same matrix. The PyMySQL rows run natively in an
environment of their own on Python 3.12 with the driver overlaid at
the requested version (`pymysql[rsa]`, so the `cryptography` package
the 1.1 lines need for the server's authentication comes along); the
mysqlclient rows run inside the docker container on 3.12, overlaying
the driver at the requested version there, since every line of it
builds from source. A test in each suite asserts the installed driver
satisfies `supports`, so a matrix entry outside the range fails
loudly rather than passing vacuously.

## Continuous integration

The workflow runs every test job on Linux with a MySQL service
container (service containers are Linux-only, and the drivers do not
differ per OS at the instrumented layer), the URL in the environment,
across Python 3.12 to 3.15 and the driver version matrix. The runner
image carries a MySQL client development package, so the
`mysqlclient` group builds there without an install step.

## Testing against unreleased wrapture

The package depends on a released wrapture. To run the tests against a
checkout of wrapture in the sibling directory ../wrapture, without
editing pyproject.toml:

```console
just test-dev
```

which overlays that checkout as an editable install for the run.

## Writing tests

- Put new test files in tests/ (package level) or tests/<target>/
  (for one instrumentation) and name them `test_*.py`.

- Import the package under test as `wrapture_instrumentation_mysql`,
  and the instrumentation classes from their subpackages. The project
  is installed into the uv-managed environment, so no path
  manipulation is needed.

- Guard the driver imports with `pytest.importorskip` at the top of
  the module (`pytest.importorskip("pymysql")`), so an environment
  without that driver skips the suite rather than erroring; the
  native environment is one such for the MySQLdb suite.

- Take the `mysql` fixture for the server and create temporary tables
  for whatever the test needs; never leave ordinary tables or
  procedures behind.

- Validate behaviour with wrapture's own unit testing layer:
  `wrapture.timeline()` to record what the instrumentation's bindings
  observe and the tape's tree and queries to assert on it, and
  `wrapture.instrumentation()` to scope an application of a class to
  a block, driving the real driver against the real server. Do not
  mock a driver and do not use `unittest.mock`. If something cannot
  be expressed that way, write it plainly with a comment naming the
  gap, and say so when summarising the work.

- Async cases run their coroutine with `asyncio.run` inside the test;
  no pytest-asyncio.

- Tests should not depend on anything in the scratch/ directory,
  which is not part of the repository.
