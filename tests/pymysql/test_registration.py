"""The entry point: resolving the instrumentation by its bare name,
and what the listing tool says about it."""

from __future__ import annotations

from importlib import metadata

import pytest

pytest.importorskip("pymysql")

import pymysql
from wrapture import Config, InstrumentEntry, instrumentation, timeline

from tests.conftest import DISTRIBUTION, Server, run_tool
from wrapture_instrumentation_mysql import __version__
from wrapture_instrumentation_mysql.pymysql import PymysqlInstrumentation


def test_the_bare_name_resolves_to_the_class() -> None:
    with instrumentation("pymysql") as record:
        (instance,) = record.instrumentations

        assert type(instance) is PymysqlInstrumentation
        assert instance.name == "pymysql"
        assert instance.distribution == DISTRIBUTION
        assert instance.description == "Query and transaction tracing for PyMySQL."


def test_a_config_entry_applies_and_reverts(mysql: Server) -> None:
    applied = Config(instrument=[InstrumentEntry("pymysql")]).apply()
    try:
        report = applied.report()
        assert "pymysql" in report
        assert f"target pymysql {metadata.version('pymysql')}" in report
        assert "applied pymysql" in report

        with timeline() as tape, pymysql.connect(**mysql.kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()

        assert [event.path for event in tape.all] == [
            "pymysql.connections:Connection.connect",
            "pymysql.cursors:Cursor.execute",
        ]
    finally:
        applied.revert()

    with timeline() as tape, pymysql.connect(**mysql.kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

    assert tape.all == []


def test_the_listing_tool_describes_the_entry() -> None:
    output = run_tool("instrumentation", "--verbose")

    assert f"pymysql  ({DISTRIBUTION} {__version__})" in output
    assert "  Query and transaction tracing for PyMySQL." in output
    assert (
        f"  target: pymysql {metadata.version('pymysql')}, supported (>=1.1.1,<2)"
        in output
    )
    assert "  modules: pymysql" in output

    # The listing pads the setting names into a column, so the name
    # and its description are checked apart.

    assert "    statement = false " in output
    assert "record the SQL text as handed to the driver on each query" in output


def test_the_toml_template_carries_the_settings() -> None:
    output = run_tool("instrumentation", "--toml")

    assert '[[instrument]]\nname = "pymysql"\nenabled = false' in output
    assert "# statement = false" in output
