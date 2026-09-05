"""The entry point: resolving the instrumentation by its bare name,
and what the listing tool says about it."""

from __future__ import annotations

import asyncio
from importlib import metadata

import pytest

pytest.importorskip("aiomysql")

import aiomysql
from wrapture import Config, InstrumentEntry, instrumentation, timeline

from tests.aiomysql.conftest import connect_kwargs
from tests.conftest import DISTRIBUTION, Server, run_tool
from wrapture_instrumentation_mysql import __version__
from wrapture_instrumentation_mysql.aiomysql import AiomysqlInstrumentation


async def select_one(mysql: Server) -> None:
    async with aiomysql.connect(**connect_kwargs(mysql)) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT 1")
            assert await cursor.fetchone() == (1,)


def test_the_bare_name_resolves_to_the_class() -> None:
    with instrumentation("aiomysql") as record:
        (instance,) = record.instrumentations

        assert type(instance) is AiomysqlInstrumentation
        assert instance.name == "aiomysql"
        assert instance.distribution == DISTRIBUTION
        assert instance.description == "Query and transaction tracing for aiomysql."


def test_a_config_entry_applies_and_reverts(mysql: Server) -> None:
    applied = Config(instrument=[InstrumentEntry("aiomysql")]).apply()
    try:
        report = applied.report()
        assert "aiomysql" in report
        assert f"target aiomysql {metadata.version('aiomysql')}" in report
        assert "applied aiomysql" in report

        with timeline() as tape:
            asyncio.run(select_one(mysql))

        assert [event.path for event in tape.all] == [
            "aiomysql.connection:Connection._connect",
            "aiomysql.cursors:Cursor.execute",
        ]
    finally:
        applied.revert()

    with timeline() as tape:
        asyncio.run(select_one(mysql))

    assert tape.all == []


def test_the_listing_tool_describes_the_entry() -> None:
    output = run_tool("instrumentation", "--verbose")

    assert f"aiomysql  ({DISTRIBUTION} {__version__})" in output
    assert "  Query and transaction tracing for aiomysql." in output
    assert (
        f"  target: aiomysql {metadata.version('aiomysql')}, supported (>=0.2,<1)"
        in output
    )
    assert "  modules: aiomysql" in output

    # The listing pads the setting names into a column, so the name
    # and its description are checked apart.

    assert "    statement = false " in output
    assert "record the SQL text as handed to the driver on each query" in output


def test_the_toml_template_carries_the_settings() -> None:
    output = run_tool("instrumentation", "--toml")

    assert '[[instrument]]\nname = "aiomysql"\nenabled = false' in output
    assert "# statement = false" in output
