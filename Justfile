# Supported Python versions. No free threaded builds: the
# instrumentation is pure Python and behaves the same on them, and a
# free threaded row would only be testing whether the drivers' own C
# extensions build without the GIL, which is their concern. The full
# list runs inside docker (test-all), whose image carries the
# toolchain the mysqlclient driver needs to build from source, so the
# machine never needs a MySQL client.
python_versions := "3.12 3.13 3.14 3.15"

# One representative PyMySQL release per supported line to run its
# suite against; the instrumentation's `supports` range is kept honest
# by these. The lock's own latest is covered by the plain `test` runs,
# so it is not repeated here.
pymysql_versions := "1.1.1 1.1.3"

# Where the compose file publishes the server for mysql-start.
mysql_port := "33069"

# List available targets.
default:
    @just --list

# Starts a throwaway MySQL container itself unless WRAPTURE_MYSQL_URL
# points at a server already running.
# Run the test suite on the default Python version; extra args go to pytest.
test *ARGS:
    uv run pytest {{ARGS}}

# Run one target's test suite, e.g. `just test-target pymysql`.
test-target TARGET *ARGS:
    uv run pytest tests/{{TARGET}} {{ARGS}}

# The wrapture[otel] overlay carries the optional OpenTelemetry
# dependencies, and WRAPTURE_OTEL makes the session fixture in
# tests/conftest.py install the OpenTelemetry sink, so everything the
# tests record exports as spans to a local OTLP endpoint
# (localhost:4318 unless OTEL_EXPORTER_OTLP_ENDPOINT says otherwise),
# for checking the spans in a backend such as otel-desktop-viewer.
# Run the test suite with the events also exported as OpenTelemetry spans.
test-otel *ARGS:
    WRAPTURE_OTEL=1 uv run --with "wrapture[otel]" pytest {{ARGS}}

# Each version gets its own environment so the default .venv is
# untouched, and only the test dependency group is installed.
# Run the test suite natively on one nominated version, e.g. `just test-python 3.13`.
test-python VERSION *ARGS:
    UV_PROJECT_ENVIRONMENT=.venv-{{VERSION}} uv run --python {{VERSION}} --no-default-groups --group test pytest {{ARGS}}

# Against the compose file's own MySQL service; extra args go to
# pytest. The first run per version fetches the interpreter and
# installs the test group into a volume; reruns reuse both.
# Run the test suite inside docker on one nominated version, e.g. `just test-docker 3.15`.
test-docker VERSION *ARGS:
    UV_PYTHON={{VERSION}} docker compose run --rm --build tests {{ARGS}}

# Run the test suite inside docker on every supported Python version.
test-docker-all *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    for version in {{python_versions}}; do
        echo "=== Python ${version} (docker) ==="
        just test-docker "${version}" {{ARGS}}
    done

# Run the test suite on every supported Python version (in docker).
test-all *ARGS:
    just test-docker-all {{ARGS}}

# The pymysql suite against one PyMySQL version, in an environment of
# its own on Python 3.12, overlaid on the test group (the `rsa` extra
# rides along, since the 1.1.x lines need it against a MySQL 8
# server). PyMySQL is pure Python, so any interpreter would do; 3.12
# keeps the version rows uniform. The lock's own version is covered by
# the plain `test` runs.
# Run the pymysql suite against one PyMySQL version, e.g. `just test-pymysql 1.1.1`.
test-pymysql VERSION *ARGS:
    UV_PROJECT_ENVIRONMENT=.venv-3.12 uv run --python 3.12 --no-default-groups --group test --with "pymysql[rsa]=={{VERSION}}" pytest tests/pymysql {{ARGS}}

# Run the pymysql suite against every version in pymysql_versions.
test-pymysql-all *ARGS:
    #!/usr/bin/env bash
    set -euo pipefail
    for version in {{pymysql_versions}}; do
        echo "=== pymysql ${version} ==="
        just test-pymysql "${version}" {{ARGS}}
    done

# Published on localhost; prints the URL to export for the demos or
# for running the tests natively against it rather than a throwaway
# container per session. The server takes about ten seconds to come
# up; --wait returns once its health check passes.
# Start the compose file's MySQL server alone.
mysql-start:
    WRAPTURE_MYSQL_PORT={{mysql_port}} docker compose up -d --wait mysql
    @echo "export WRAPTURE_MYSQL_URL=mysql://root:mysql@localhost:{{mysql_port}}/wrapture"

# Stop the compose file's MySQL server (and the tests container).
mysql-stop:
    docker compose down

# The test fixture names its throwaway containers wrapture-mysql-<pid>
# and removes them itself; an interrupted run can leave one behind.
# Remove any throwaway server container an interrupted test run left behind.
mysql-clean:
    #!/usr/bin/env bash
    set -euo pipefail
    containers="$(docker ps -aq --filter name=wrapture-mysql-)"
    if [ -n "${containers}" ]; then
        docker rm -f ${containers}
    fi

# A table, inserts (one executemany), a select through a DictCursor,
# a stored procedure, a transaction with a rollback and a failing
# statement, with and without SQL text recording; the live event
# stream, then the reconstructed tree. The wrapture[otel] overlay
# carries the optional OpenTelemetry dependencies, so `just
# demo-pymysql --otel` also exports the events as spans to a local
# OTLP endpoint (localhost:4318 unless OTEL_EXPORTER_OTLP_ENDPOINT
# says otherwise).
# Drive PyMySQL against the server WRAPTURE_MYSQL_URL names with the instrumentation applied.
demo-pymysql *ARGS:
    uv run --with "wrapture[otel]" python -m demo.pymysql {{ARGS}}

# The package depends on a released wrapture. This overlays a checkout
# of wrapture from the sibling directory as an editable install for the
# run, for iterating against unreleased wrapture changes without
# touching pyproject.toml.
# Run the test suite against the wrapture checkout in ../wrapture.
test-dev *ARGS:
    uv run --with-editable ../wrapture pytest {{ARGS}}

# Check code with the ruff linter and formatter.
lint:
    uv run ruff check src tests demo
    uv run ruff format --check src tests demo

# Reformat code and fix lint issues that are auto-fixable.
format:
    uv run ruff format src tests demo
    uv run ruff check --fix src tests demo

# Type check the project with mypy.
typecheck:
    uv run mypy

# Build the source distribution and wheel into dist/.
build:
    uv build

# Remove temporary files: caches, virtual environments and build artifacts.
clean:
    rm -rf .venv .venv-*
    rm -rf build dist src/*.egg-info *.egg-info
    rm -rf .pytest_cache .mypy_cache .ruff_cache
    find . -type d -name __pycache__ -not -path "./scratch/*" -exec rm -rf {} +
