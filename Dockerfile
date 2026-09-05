# The environment the test matrix runs in: Debian with uv, plus what
# mysqlclient needs to build from its source distribution, since it
# ships no Linux or macOS wheels: pkg-config, the MySQL client
# development package (Debian's default-libmysqlclient-dev, which is
# MariaDB Connector/C) and a C compiler. Nothing else is baked in: uv
# fetches whichever interpreter the run asks for and installs the
# test dependencies into a per-version environment, both on named
# volumes so they persist across runs (see compose.yml), so the build
# of mysqlclient happens once per version. The repository is
# bind-mounted at run time, so the image never goes stale as the code
# changes.

FROM ghcr.io/astral-sh/uv:debian-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        default-libmysqlclient-dev pkg-config gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
