# Agent guidance for wrapture-instrumentation-mysql

## Project

wrapture-instrumentation-mysql is packaged instrumentation for the
MySQL client libraries (PyMySQL and mysqlclient, imported as
`MySQLdb`, now; aiomysql to follow), applied through wrapture. A
database driver is the kind of target the separate-package rule was
drawn for: the core wrapture-instrumentation package covers only
targets testable in-process with no separate backend, and this
package's tests need a real MySQL server, which the suite runs in a
docker container. See README.md for what the project provides and
how it is used, and the "Instrumentation packages" page of the
wrapture documentation for the contract every class here honours.

The package uses a src layout: the code lives in
src/wrapture_instrumentation_mysql/, one subpackage per target, plus
common.py, shared by every target, holding the database contract
keys, the operation name derived from SQL and the capture policy.

Tests live in the tests/ directory, one subdirectory per target. See
TESTING.md for where tests are, how to run them, the server they run
against, and the conventions for adding new ones.

The scratch/ directory is ignored by git. It holds temporary working
files; never reference scratch/ files by name from code or
documentation that will be committed.

## Rules specific to instrumentation

- The module that defines an `Instrumentation` subclass (the target
  subpackage's `__init__.py`) imports wrapture and nothing else (the
  package's own common.py, which itself imports only wrapture, is
  the one exception). Everything that touches the driver lives in
  sibling modules imported inside the hook (`from . import cursor`),
  themselves importing only wrapture at top level and reaching the
  driver through the module the hook is handed or a lazy import
  inside a function. wrapture loads the class when the config loads,
  before the application imports anything, and a class whose module
  imported its target would drag the target in ahead of the hook
  meant to fire on its import.

- No target is ever a dependency in pyproject.toml. The only runtime
  dependency is wrapture. The drivers go in the `test` dependency
  group, except mysqlclient, which builds from source against a MySQL
  client library and so has a dependency group of its own
  (`mysqlclient`) that only the docker test container and CI install;
  every suite `importorskip`s its driver so an environment without it
  skips that suite rather than errors. Never depend on the packages
  being instrumented.

- Targets use bare subpackage directories (`pymysql/`), not the core
  package's `<category>/<target>` role directories: every target here
  is a database driver, so a category segment would say nothing.
  Entry point names are always the import name of the package the
  hooks live under (`pymysql`; mysqlclient's is `MySQLdb`, the name
  it is imported and configured by, not the name it is installed
  by), with the subpackage directory lowercased (`mysqldb/`).

- Every event carries the database contract keys from common.py:
  `system` ("mysql"), `operation`, and the `database`, `host` and
  `port` the connection reached. The SQL text is recorded only when
  the `statement` setting is on, and that setting defaults to False
  on every target (one rule across the collection: a driver cannot
  tell a literal an application interpolated from a placeholder).
  Bound parameters are never recorded under any setting, and a
  parameter sequence handed to executemany is never iterated by the
  capture, since it may be a generator the driver has yet to consume.
  Connect arguments are never captured: they carry the password.

- The bindings sit on the drivers' public DBAPI methods, above their
  client-side parameter interpolation, so a recorded statement is the
  template with its placeholders. Every binding is a leaf: the
  drivers call their own public methods internally (`executemany`
  runs `execute` per batch, `connect()` runs `SET` statements through
  a cursor), and the leaf silences those inner calls so each
  application call is one event.

- Every target has its own test suite under `tests/<target>/`,
  runnable alone, and a Justfile recipe to run it against several
  lines of its driver (the class's `supports` range is kept honest
  against them).

- Tests validate behaviour with wrapture's own unit testing layer
  (timeline tapes and their queries, `wrapture.instrumentation()` for
  scoping) driving the real driver against a real MySQL server: the
  shared `mysql` fixture in tests/conftest.py uses the server
  WRAPTURE_MYSQL_URL names, or runs a throwaway container itself, and
  fails (never skips) when it can do neither. Never mock a driver,
  and never use `unittest.mock`. Tests isolate themselves with
  temporary tables (`CREATE TEMPORARY TABLE`), private to their
  connection and gone when it closes; what cannot be temporary (a
  stored procedure, a table two connections must both see) gets a
  unique name and is dropped in a finally. When a test wants
  something wrapture's layer cannot express, write it the plain way
  with a comment naming the gap and call the gap out in the summary
  of the work.

- The instrumentation binds the drivers' Python seams and never
  anything at or below their wire protocols (PyMySQL's packet layer,
  mysqlclient's C core), which is where the seams stop.

- Nothing MySQL, server or client library, is ever installed on the
  development machine: the server runs in docker, and the driver that
  needs a client library to build (mysqlclient) is built only inside
  the docker test image and on CI.

- Docs for the instrumentation live in the per-target README.md under
  its subpackage, linked from the table in the top README. This
  repository has README.md and CHANGES.md; the module docstrings stay
  contributor-facing implementation commentary.

## Tooling: always use uv

All Python environment and package management in this project is done
with [uv](https://docs.astral.sh/uv/). Never use the Python venv
module, bare pip, or python -m build directly.

- Run commands in the project environment: `uv run <command>`
  (e.g. `uv run pytest`)

- Run a Python interpreter: `uv run python`

- Build sdist and wheel: `uv build`

- Add or remove dependencies (updates pyproject.toml): `uv add <package>`,
  `uv remove <package>`

- Sync the environment from pyproject.toml: `uv sync`

## Common tasks: use the Justfile

The Justfile defines targets for the common development tasks,
wrapping the correct uv invocations. Prefer these targets over
synthesizing the underlying commands yourself; run `just --list` to
see everything.

- `just test` runs the whole test suite on the default Python
  version. Extra arguments pass through to pytest, so a specific file
  or test is `just test tests/pymysql/test_recording.py` or
  `just test -k pattern`.

- `just test-target pymysql` runs one target's suite.

- `just test-docker 3.15` runs the suite inside docker on one
  nominated Python version, against the compose file's own server
  and with every driver installed; `just test-all` runs it that way
  on every supported version. `just test-python 3.13` runs natively
  instead, without the MySQLdb suite.

- `just test-pymysql 1.1.1` runs the pymysql suite against one
  PyMySQL line, natively; `just test-mysqldb 2.2.1` runs the MySQLdb
  suite against one mysqlclient line, inside docker; the `-all`
  forms loop over the lists.

- `just mysql-start` runs the compose file's server alone, published
  on localhost, and prints the URL to export for the demos or for
  running the tests against it; `just mysql-stop` stops it;
  `just mysql-clean` removes any throwaway container an interrupted
  test run left behind.

- `just test-dev` runs the suite against an editable checkout of
  wrapture in the sibling directory ../wrapture, for iterating against
  unreleased wrapture changes without editing pyproject.toml.

- `just lint` checks with the ruff linter and formatter; `just format`
  reformats and applies auto-fixes.

- `just typecheck` runs mypy.

## Style

- Do not use emdashes in any files in this project. Rephrase with
  commas, parentheses, colons, or separate sentences instead.

- In bulleted lists where items run to multiple lines, put a blank
  line between the bullets: in docstrings, markdown files, and any
  other prose. This is about the raw file being readable, not the
  rendered form, which can look fine either way. Be consistent within
  a list: if one item needs the spacing, space every item in that
  list, never a mix.

- Project code must always use Python type hints. Add them to all
  function and method signatures (parameters and return types), and
  to attributes and variables where the type is not obvious from the
  assignment. When adding or modifying code that lacks type hints,
  add them.

- Use vertical white space liberally inside function and method
  bodies. Write code in paragraphs: group the statements that
  together perform one step, and separate each group from the next
  with a blank line. Natural paragraph boundaries include setup
  versus the main work versus the result, before and after a
  conditional or loop, and around a with or try block. Do not cram a
  body into one contiguous blob, and equally do not put a blank line
  between every single statement; the blank lines should mark where
  one thought ends and the next begins.

- Where it helps the reader, start a paragraph of code with a short
  comment saying what that step does or why it is needed. Prefer one
  comment per logical block over line-by-line commentary, and skip
  the comment entirely when the code already says it plainly.

- Put a blank line between such a block comment and the code below
  it: the comment introduces the paragraph rather than sitting flush
  against its first line.

- Put a blank line between a function or method docstring and the
  first line of code in the body.

- Every function, method or property that is part of the public API
  must have a docstring saying what it does. The exceptions are cases
  that are truly trivial and obvious, such as an accessor property
  named for the attribute it returns, and dunder methods implementing
  standard protocols.

## Git

- The repository follows a main/develop split: develop is the
  working and default branch, main holds releases, and feature
  branches merge to develop.

- Git commit messages must never include a co-authored-by agent
  message or any similar agent attribution trailer.

- An AI agent must never commit changes on its own initiative. Finish
  the piece of work, summarize it, and wait to be told to commit.
  Permission to commit applies only to the work it was given for; it
  does not carry forward to later steps of a multi-step plan, each of
  which needs its own review and its own instruction to commit.
  Uncommitted changes are how the review happens: once work is
  committed it can no longer be reviewed as the pending diff, so
  committing early makes review harder, not easier.

- When merging a feature branch back to develop and pushing to the
  remote, do not treat the work as landed until the CI workflow on
  GitHub has run against the pushed merge and passed. Check the run
  and only once it is green report that the changes are on the remote
  and clean up the feature branch. If CI fails, leave the feature
  branch in place, report the failure, and wait for instructions
  rather than deleting anything.
