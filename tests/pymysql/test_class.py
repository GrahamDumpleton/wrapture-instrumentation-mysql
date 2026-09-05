"""The class as wrapture reads it: its data, its settings, and the
installed PyMySQL satisfying its supports range."""

from __future__ import annotations

import warnings
from importlib import metadata

import pytest

pytest.importorskip("pymysql")

# pymysql is imported for its side: the class's trigger fires on its
# import, so the applying test below works with this file run on its
# own.
import pymysql  # noqa: F401
from wrapture import ConfigError, ConfigWarning, instrumentation

from wrapture_instrumentation_mysql.pymysql import PymysqlInstrumentation


def test_class_data() -> None:
    assert PymysqlInstrumentation.target == "pymysql"
    assert PymysqlInstrumentation.removable is True
    assert PymysqlInstrumentation.requires == ()
    assert PymysqlInstrumentation.supports == ">=1.1.1,<2"

    assert set(PymysqlInstrumentation.settings) == {"statement"}
    assert PymysqlInstrumentation.settings["statement"].default is False


def test_the_description_is_the_docstring_first_line() -> None:
    assert (PymysqlInstrumentation.__doc__ or "").splitlines()[0] == (
        "Query and transaction tracing for PyMySQL."
    )


def test_constructing_without_settings_works() -> None:
    instance = PymysqlInstrumentation()

    assert instance.settings == {"statement": False}
    assert instance.applied == ()
    assert instance.pending == ("pymysql",)


def test_an_undeclared_setting_is_refused() -> None:
    with pytest.raises(ConfigError, match="leaf"):
        PymysqlInstrumentation(leaf=False)


def test_a_setting_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(ConfigError, match="statement"):
        PymysqlInstrumentation(statement="yes")


def test_the_installed_pymysql_is_within_supports() -> None:
    # wrapture gates on supports before firing any trigger and warns,
    # never errors, when the version is outside it; make that warning
    # an error here so a matrix entry outside the range fails loudly
    # instead of passing with nothing applied.

    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigWarning)

        with instrumentation(PymysqlInstrumentation) as record:
            (applied,) = record.instrumentations

            assert applied.target_version == metadata.version("pymysql")
            assert applied.applied == ("pymysql",)
            assert applied.pending == ()
