"""The class as wrapture reads it: its data, its settings, and the
installed mysqlclient satisfying its supports range."""

from __future__ import annotations

import warnings
from importlib import metadata

import pytest

pytest.importorskip("MySQLdb")

# The submodules the seams live in are imported for their side: the
# class's triggers fire on their import, so the applying test below
# works with this file run on its own (importing the package alone
# loads neither of them).
import MySQLdb.connections  # noqa: F401
import MySQLdb.cursors  # noqa: F401
from wrapture import ConfigError, ConfigWarning, instrumentation

from wrapture_instrumentation_mysql.mysqldb import MySQLdbInstrumentation


def test_class_data() -> None:
    assert MySQLdbInstrumentation.target == "MySQLdb"
    assert MySQLdbInstrumentation.removable is True
    assert MySQLdbInstrumentation.requires == ()
    assert MySQLdbInstrumentation.supports == ">=2.2.1,<3"

    assert set(MySQLdbInstrumentation.settings) == {"statement"}
    assert MySQLdbInstrumentation.settings["statement"].default is False


def test_the_description_is_the_docstring_first_line() -> None:
    assert (MySQLdbInstrumentation.__doc__ or "").splitlines()[0] == (
        "Query and transaction tracing for mysqlclient (MySQLdb)."
    )


def test_constructing_without_settings_works() -> None:
    instance = MySQLdbInstrumentation()

    assert instance.settings == {"statement": False}
    assert instance.applied == ()
    assert instance.pending == ("MySQLdb.cursors", "MySQLdb.connections")


def test_an_undeclared_setting_is_refused() -> None:
    with pytest.raises(ConfigError, match="leaf"):
        MySQLdbInstrumentation(leaf=False)


def test_a_setting_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(ConfigError, match="statement"):
        MySQLdbInstrumentation(statement="yes")


def test_the_installed_mysqlclient_is_within_supports() -> None:
    # wrapture gates on supports before firing any trigger and warns,
    # never errors, when the version is outside it; make that warning
    # an error here so a matrix entry outside the range fails loudly
    # instead of passing with nothing applied. The version is the
    # mysqlclient distribution's, resolved from the MySQLdb import
    # name.

    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigWarning)

        with instrumentation(MySQLdbInstrumentation) as record:
            (applied,) = record.instrumentations

            assert applied.target_version == metadata.version("mysqlclient")
            assert applied.applied == ("MySQLdb.cursors", "MySQLdb.connections")
            assert applied.pending == ()
