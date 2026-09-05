"""Helpers shared by every suite, the MySQL server the suites need,
and the optional live printer."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import metadata
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import wrapture

DISTRIBUTION = "wrapture-instrumentation-mysql"

# The server the fixture starts when none is named: the official image
# at a pinned minor (8.4 is the current long-term line), since nothing
# recorded depends on the server version and a surprise major should
# not change behaviour under the suite silently.
IMAGE = "mysql:8.4"
PASSWORD = "mysql"
DATABASE = "wrapture"
READY_TIMEOUT = 90.0


def run_python(*arguments: str) -> str:
    """Run a fresh interpreter with the given arguments and return its
    stdout, failing the test with its stderr if it did not exit 0.

    For the properties that are about a fresh interpreter's state,
    what `import` pulls in and what the listing tool prints; the
    environment is the test environment's own, so the package and
    its entry points are installed.
    """

    completed = subprocess.run(
        [sys.executable, *arguments],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def run_snippet(code: str) -> str:
    """Run a snippet of Python in a fresh interpreter; see run_python."""

    return run_python("-c", code)


def run_tool(*arguments: str) -> str:
    """Run `python -m wrapture.tools` with the given arguments in a
    fresh interpreter; see run_python."""

    return run_python("-m", "wrapture.tools", *arguments)


def registered_entry_points() -> list[metadata.EntryPoint]:
    """The `wrapture.instrumentation` entry points this distribution
    registers, in entry point order."""

    points = metadata.entry_points(group="wrapture.instrumentation")

    return [
        point
        for point in points
        if point.dist is not None
        and point.dist.name.replace("_", "-").lower() == DISTRIBUTION
    ]


@dataclass(frozen=True)
class Server:
    """Where the MySQL server the tests use is, in the spellings the
    drivers take: a URL, or keyword arguments."""

    host: str
    port: int
    user: str
    password: str
    database: str

    @property
    def url(self) -> str:
        """The URL form, `mysql://user:password@host:port/database`,
        the spelling of the environment variable (none of the drivers
        takes a URL themselves)."""

        return (
            f"mysql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )

    @property
    def kwargs(self) -> dict[str, Any]:
        """The keyword argument form the drivers' connect functions
        take (`host`, `port`, `user`, `password`, `database`)."""

        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
        }

    @classmethod
    def from_url(cls, url: str) -> Server:
        """Parse the URL form back into its parts."""

        parts = urlsplit(url)

        return cls(
            host=parts.hostname or "localhost",
            port=parts.port or 3306,
            user=unquote(parts.username or "root"),
            password=unquote(parts.password or ""),
            database=parts.path.lstrip("/") or DATABASE,
        )


def docker_available() -> bool:
    """Whether a docker CLI is on the path and its daemon answers."""

    if shutil.which("docker") is None:
        return False

    try:
        completed = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False

    return completed.returncode == 0


def start_container(name: str) -> tuple[str, int]:
    """Run a throwaway server container published on a random
    localhost port and return the container id and that port."""

    completed = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--publish",
            "127.0.0.1::3306",
            "--env",
            f"MYSQL_ROOT_PASSWORD={PASSWORD}",
            "--env",
            f"MYSQL_DATABASE={DATABASE}",
            "--name",
            name,
            IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(f"MySQL: docker run failed:\n{completed.stderr}")

    container = completed.stdout.strip()

    published = subprocess.run(
        ["docker", "port", container, "3306/tcp"],
        capture_output=True,
        text=True,
        check=False,
    )
    if published.returncode != 0:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        pytest.fail(f"MySQL: docker port failed:\n{published.stderr}")

    # One line per address family; the port is the same on each.

    first = published.stdout.strip().splitlines()[0]
    port = int(first.rsplit(":", 1)[1])

    return container, port


def wait_until_ready(server: Server) -> None:
    """Connect from the host until the server answers a query.

    A real host connection is the honest probe: the official image
    first runs a temporary server on the unix socket alone to
    initialise the data directory, which a ping inside the container
    would answer for, and only then starts the real one on TCP, about
    ten seconds after the container does. PyMySQL is in the test
    group on every row, so it does the probing; and its connection as
    root is also the first full caching_sha2_password exchange for
    that user, after which the server caches the credential and every
    later connection by any driver in the session takes the fast
    path.
    """

    import pymysql

    deadline = time.monotonic() + READY_TIMEOUT
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            connection = pymysql.connect(connect_timeout=3, **server.kwargs)
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
            finally:
                connection.close()
            return
        except pymysql.MySQLError as error:
            last_error = error
            time.sleep(0.5)

    pytest.fail(
        f"MySQL: server at {server.host}:{server.port} never became ready: {last_error}"
    )


@pytest.fixture(scope="session")
def mysql() -> Iterator[Server]:
    """The MySQL server the suites drive the drivers against.

    WRAPTURE_MYSQL_URL names a server already running (CI's service
    container, the compose file's `mysql` service, or one of your
    own) and nothing is started. Otherwise a throwaway container is
    run from the official image on a random localhost port and
    removed at the end of the session. With neither a URL nor a
    docker daemon the session fails rather than skipping, so a green
    run means the suites ran.
    """

    url = os.environ.get("WRAPTURE_MYSQL_URL")

    if url:
        server = Server.from_url(url)
        wait_until_ready(server)
        yield server
        return

    if not docker_available():
        pytest.fail(
            "MySQL: WRAPTURE_MYSQL_URL is not set and no docker daemon is"
            " reachable; start Docker Desktop (the suite then runs its own"
            " throwaway server) or point the variable at a server"
        )

    container, port = start_container(f"wrapture-mysql-{os.getpid()}")
    try:
        server = Server("127.0.0.1", port, "root", PASSWORD, DATABASE)
        wait_until_ready(server)
        yield server
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


@pytest.fixture(autouse=True, scope="session")
def printer() -> Iterator[None]:
    """Stream every recorded event live to stderr when WRAPTURE_PRINTER
    is set in the environment, for visually verifying what the tests
    record; pytest captures stderr, so run with -s to see it:

        WRAPTURE_PRINTER=1 just test tests/pymysql -s

    A process-wide sink is consulted alongside the tests' own scoped
    tapes, so the stream shows exactly what each tape hears without
    disturbing any assertion.
    """

    if not os.environ.get("WRAPTURE_PRINTER"):
        yield
        return

    sink = wrapture.Printer()
    wrapture.add_sink(sink)

    try:
        yield
    finally:
        wrapture.remove_sink(sink)


@pytest.fixture(autouse=True, scope="session")
def otel() -> Iterator[None]:
    """Export every recorded event as OpenTelemetry spans over OTLP
    when WRAPTURE_OTEL is set in the environment, for verifying what
    the tests record in a local backend such as otel-desktop-viewer
    (http://localhost:4318, or wherever OTEL_EXPORTER_OTLP_ENDPOINT
    points); the spans are flushed when the session ends:

        WRAPTURE_OTEL=1 just test-otel tests/pymysql

    The Justfile recipe overlays the wrapture[otel] dependencies for
    the run; without them the fixture fails the session with guidance
    rather than silently exporting nothing.
    """

    if not os.environ.get("WRAPTURE_OTEL"):
        yield
        return

    try:
        import wrapture.otel
    except ImportError:
        pytest.fail(
            "WRAPTURE_OTEL is set but the OpenTelemetry dependencies are not"
            " installed; run through `just test-otel`, which overlays"
            " wrapture[otel] for the run"
        )

    sink = wrapture.otel.sink(service_name="wrapture-instrumentation-mysql")
    wrapture.add_sink(sink)

    try:
        yield
    finally:
        wrapture.flush_sinks()
        wrapture.remove_sink(sink)
