# syntax=docker/dockerfile:1

# ── Builder ────────────────────────────────────────────────────────────────────
# Wheels are built here and installed offline below, so the runtime stage never
# needs a package index and never re-resolves dependencies: the set of files that
# got tested is the set that ships.
#
# `pip wheel` runs after `COPY src`, so editing any source file invalidates this
# layer and the next build re-downloads every dependency. Splitting the download
# from the build (a stub package resolved before the real `COPY src`) would cache
# them across edits; it was left out because the stub needs an `|| true` that can
# silently produce two wheels of the same version, and a slow rebuild is a much
# smaller problem than a wrong install.
FROM python:3.12-slim-bookworm AS builder

# The Aliyun mirror is an order of magnitude faster from CN networks. It is an ARG
# rather than an ENV so `--build-arg PIP_INDEX_URL=...` can point elsewhere without
# editing this file, and so the mirror is not baked into the image where it would
# silently redirect every future install.
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# `readme = "README.md"` in pyproject makes it part of the metadata, and the
# src layout is what setuptools packages.  `.env` is neither copied nor needed:
# config.py only falls back to a .env *next to the project root*, which inside a
# site-packages install resolves to a path that does not exist.  Every setting is
# therefore an environment variable supplied at run time.
COPY pyproject.toml README.md ./
COPY src ./src

# The two `rhosocial-*` ORM packages are on PyPI, but the release branches run one
# version ahead of the last upload (dev30 / dev17 vs dev29 / dev16), and a
# developer's venv holds the editable checkout of those branches — so an image
# built from the index alone would run different ORM code than the tests were
# written against.  `docker/build-wheels.sh` builds both into `wheels/`, and pip
# prefers the highest version across the index and every `--find-links` directory,
# so dev30 and dev17 outrank the published dev29 and dev16.  That relies on version
# ordering rather than on the local copies being authoritative, which is why the
# script and the release branches have to keep their versions moving forward.
#
# A missing `wheels/` is only a warning to pip, which then falls back to the index
# — acceptable, and better than failing a build made on a machine with no checkouts.
COPY wheels/ ./wheels/

RUN pip wheel --index-url "${PIP_INDEX_URL}" --find-links=wheels --wheel-dir /wheels .


# ── Runtime ────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# `psycopg[binary]` bundles libpq, so no libpq-dev and no postgresql-client are
# needed here — the image carries no database tooling at all.
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels schedule-manager \
    && rm -rf /wheels

# Unprivileged: the server holds the database credentials and serves
# unauthenticated requests, so it should not be able to modify the interpreter,
# the installed package, or its own image. /app stays root-owned for the same
# reason — the server writes nothing there, and a writable working directory is
# only useful to something that is already compromised.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin schedule \
    && mkdir -p /app
USER schedule
WORKDIR /app

# 0.0.0.0 is not a default in config.py — it falls back to 127.0.0.1, which inside a
# container is unreachable from outside.  The image has to fix that, or every
# deployment starts with a server nobody can talk to.
ENV SCHEDULE_BIND_HOST=0.0.0.0 \
    SCHEDULE_BIND_PORT=8000

# Startup must not issue the DDL: replicas boot together and would contend on the
# same locks.  Run `schedule-manager-setup-db` as a deploy step instead.
ENV SCHEDULE_AUTO_MIGRATE=0

EXPOSE 8000

# The probe hits /healthz, which pings the database, so "running" and "able to serve"
# cannot drift apart.  No curl in a slim image, and the bind host has to be mapped
# back from 0.0.0.0 to something connectable.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, sys, urllib.request as u; h = os.environ['SCHEDULE_BIND_HOST']; h = '127.0.0.1' if h in ('0.0.0.0', '::', '') else h; p = os.environ['SCHEDULE_BIND_PORT']; sys.exit(0 if u.urlopen('http://%s:%s/healthz' % (h, p), timeout=3).status == 200 else 1)"]

# Exec form, so SIGTERM reaches uvicorn and the lifespan's `close_pool` runs; a shell
# wrapper here would leave connections to Postgres to be reaped by the OS.
CMD ["schedule-manager-mcp"]
