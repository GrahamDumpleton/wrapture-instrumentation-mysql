"""The entry point: resolving the instrumentation by its bare name,
and what the listing tool says about it."""

from __future__ import annotations

from importlib import metadata

import pytest

pytest.importorskip("MySQLdb")

import MySQLdb
from wrapture import Config, InstrumentEntry, instrumentation, timeline

from tests.conftest import DISTRIBUTION, Server, run_tool
from wrapture_instrumentation_mysql import __version__
from wrapture_instrumentation_mysql.mysqldb import MySQLdbInstrumentation


def test_the_bare_name_resolves_to_the_class() -> None:
    with instrumentation("MySQLdb") as record:
        (instance,) = record.instrumentations

        assert type(instance) is MySQLdbInstrumentation
        assert instance.name == "MySQLdb"
        assert instance.distribution == DISTRIBUTION
        assert instance.description == (
            "Query and transaction tracing for mysqlclient (MySQLdb)."
        )


def test_a_config_entry_applies_and_reverts(mysql: Server) -> None:
    applied = Config(instrument=[InstrumentEntry("MySQLdb")]).apply()
    try:
        report = applied.report()
        assert "MySQLdb" in report
        assert f"target MySQLdb {metadata.version('mysqlclient')}" in report
        assert "applied MySQLdb.cursors, MySQLdb.connections" in report

        with timeline() as tape, MySQLdb.connect(**mysql.kwargs) as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()

        assert [event.path for event in tape.all] == [
            "MySQLdb.connections:Connection.__init__",
            "MySQLdb.cursors:BaseCursor.execute",
        ]
    finally:
        applied.revert()

    with timeline() as tape, MySQLdb.connect(**mysql.kwargs) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()

    assert tape.all == []


def test_the_listing_tool_describes_the_entry() -> None:
    output = run_tool("instrumentation", "--verbose")

    assert f"MySQLdb  ({DISTRIBUTION} {__version__})" in output
    assert "  Query and transaction tracing for mysqlclient (MySQLdb)." in output
    assert (
        f"  target: MySQLdb {metadata.version('mysqlclient')}, supported (>=2.2.1,<3)"
        in output
    )
    assert "  modules: MySQLdb.cursors, MySQLdb.connections" in output

    # The listing pads the setting names into a column, so the name
    # and its description are checked apart.

    assert "    statement = false " in output
    assert "record the SQL text as handed to the driver on each query" in output


def test_the_toml_template_carries_the_settings() -> None:
    output = run_tool("instrumentation", "--toml")

    assert '[[instrument]]\nname = "MySQLdb"\nenabled = false' in output
    assert "# statement = false" in output
