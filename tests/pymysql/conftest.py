"""Fixtures for the pymysql suite: the instrumentation applied and a
scoped tape, over the shared server fixture."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

# PyMySQL is the suite's own test dependency; a build without it
# skips rather than errors.
pytest.importorskip("pymysql")

from wrapture import Tape, instrumentation, timeline

from wrapture_instrumentation_mysql.pymysql import PymysqlInstrumentation


@pytest.fixture
def tape() -> Iterator[Tape]:
    with instrumentation(PymysqlInstrumentation), timeline() as recorded:
        yield recorded
