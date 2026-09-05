"""Fixtures for the MySQLdb suite: the instrumentation applied and a
scoped tape, over the shared server fixture."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

# mysqlclient is the suite's own test dependency, in a dependency
# group of its own since it builds from source against a MySQL client
# library; a build without it skips rather than errors, visibly.
pytest.importorskip("MySQLdb")

from wrapture import Tape, instrumentation, timeline

from wrapture_instrumentation_mysql.mysqldb import MySQLdbInstrumentation


@pytest.fixture
def tape() -> Iterator[Tape]:
    with instrumentation(MySQLdbInstrumentation), timeline() as recorded:
        yield recorded
