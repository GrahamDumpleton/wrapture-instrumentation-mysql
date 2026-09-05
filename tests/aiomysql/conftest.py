"""Fixtures for the aiomysql suite: the instrumentation applied and a
scoped tape, over the shared server fixture; and the server's
keyword arguments in aiomysql's spelling."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

# aiomysql is the suite's own test dependency; a build without it
# skips rather than errors.
pytest.importorskip("aiomysql")

from wrapture import Tape, instrumentation, timeline

from tests.conftest import Server
from wrapture_instrumentation_mysql.aiomysql import AiomysqlInstrumentation


def connect_kwargs(mysql: Server) -> dict[str, Any]:
    """The server's keyword arguments as aiomysql takes them: `db`
    where the other drivers say `database`."""

    kwargs = dict(mysql.kwargs)
    kwargs["db"] = kwargs.pop("database")

    return kwargs


@pytest.fixture
def tape() -> Iterator[Tape]:
    with instrumentation(AiomysqlInstrumentation), timeline() as recorded:
        yield recorded
