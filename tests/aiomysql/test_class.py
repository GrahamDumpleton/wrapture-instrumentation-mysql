"""The class as wrapture reads it: its data, its settings, and the
installed aiomysql satisfying its supports range."""

from __future__ import annotations

import warnings
from importlib import metadata

import pytest

pytest.importorskip("aiomysql")

# aiomysql is imported for its side: the class's trigger fires on its
# import, so the applying test below works with this file run on its
# own.
import aiomysql  # noqa: F401
from wrapture import ConfigError, ConfigWarning, instrumentation

from wrapture_instrumentation_mysql.aiomysql import AiomysqlInstrumentation


def test_class_data() -> None:
    assert AiomysqlInstrumentation.target == "aiomysql"
    assert AiomysqlInstrumentation.removable is True
    assert AiomysqlInstrumentation.requires == ()
    assert AiomysqlInstrumentation.supports == ">=0.2,<1"

    assert set(AiomysqlInstrumentation.settings) == {"statement"}
    assert AiomysqlInstrumentation.settings["statement"].default is False


def test_the_description_is_the_docstring_first_line() -> None:
    assert (AiomysqlInstrumentation.__doc__ or "").splitlines()[0] == (
        "Query and transaction tracing for aiomysql."
    )


def test_constructing_without_settings_works() -> None:
    instance = AiomysqlInstrumentation()

    assert instance.settings == {"statement": False}
    assert instance.applied == ()
    assert instance.pending == ("aiomysql",)


def test_an_undeclared_setting_is_refused() -> None:
    with pytest.raises(ConfigError, match="leaf"):
        AiomysqlInstrumentation(leaf=False)


def test_a_setting_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(ConfigError, match="statement"):
        AiomysqlInstrumentation(statement="yes")


def test_the_installed_aiomysql_is_within_supports() -> None:
    # wrapture gates on supports before firing any trigger and warns,
    # never errors, when the version is outside it; make that warning
    # an error here so a matrix entry outside the range fails loudly
    # instead of passing with nothing applied.

    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigWarning)

        with instrumentation(AiomysqlInstrumentation) as record:
            (applied,) = record.instrumentations

            assert applied.target_version == metadata.version("aiomysql")
            assert applied.applied == ("aiomysql",)
            assert applied.pending == ()
