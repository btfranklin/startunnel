# syntax=docker/dockerfile:1.12

ARG PYTHON_BASE_IMAGE="python:3.15.1-alpine3.23@sha256:cb295b554dbbea09b2c0e6e6d9ce55f051347c902fbb611e618c071addfb30a3"
ARG PYTHON_TEST_BASE_IMAGE="python:3.15.1-slim-trixie@sha256:533b0b2f9b5d46fd097091cf826f27902f1d6c3bca714b6dc74551356aa97a51"
ARG RUST_BUILD_IMAGE="rust:1.99-slim-trixie@sha256:4df2ae8cfdfebdfc13a65da7f253b73d68983dc0339f2719bc17a279c913b13d"

# Build the existing Perfect Doc release for the test image's architecture.
# The published release has no Linux ARM binary. The compiler stays in this stage.
FROM ${RUST_BUILD_IMAGE} AS documentation-tools

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && cargo install --git https://github.com/btfranklin/perfect-doc \
        --rev 6c22468c4bfc14d672182e01c4b4d8959d0093d5 \
        --locked --root /opt/perfect-doc

FROM ${PYTHON_BASE_IMAGE} AS python-base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PDM_CHECK_UPDATE=false \
    PDM_IGNORE_SAVED_PYTHON=1 \
    PDM_USE_VENV=1 \
    PDM_VENV_IN_PROJECT=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN apk upgrade --no-cache \
    && apk add --no-cache "zlib>=1.3.2-r1"

FROM python-base AS dependency-builder

ARG PDM_VERSION="2.29.3"

RUN python -m pip install --no-cache-dir "pdm==${PDM_VERSION}"

COPY pyproject.toml pdm.lock ./

FROM dependency-builder AS production-dependencies

RUN pdm install --prod --frozen-lockfile --no-editable \
    && find .venv -type d -name __pycache__ -prune -exec rm -rf '{}' +

# Playwright ships glibc browser binaries. Test tools are separate from the
# musl application image and exercise the Alpine web/migration/maintenance services.
FROM ${PYTHON_TEST_BASE_IMAGE} AS test-dependency-builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PDM_CHECK_UPDATE=false \
    PDM_IGNORE_SAVED_PYTHON=1 \
    PDM_USE_VENV=1 \
    PDM_VENV_IN_PROJECT=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app
ARG PDM_VERSION="2.29.3"
RUN apt-get update \
    && apt-get install --no-install-recommends --only-upgrade --yes openssl libssl3t64 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir "pdm==${PDM_VERSION}"
COPY pyproject.toml pdm.lock ./

FROM test-dependency-builder AS development-dependencies

RUN pdm install --frozen-lockfile --no-editable -G dev

FROM test-dependency-builder AS agent-dependencies

RUN pdm install --frozen-lockfile --no-editable -G dev -G live-agents

FROM production-dependencies AS application-builder

COPY manage.py ./
COPY src ./src
COPY docs ./docs
COPY examples ./examples
COPY cli ./cli
COPY generated ./generated
COPY static ./static
COPY templates ./templates
COPY scripts/stamp_client.py /tmp/stamp_client.py

ARG STARTUNNEL_CLIENT_VERSION=unreleased
ARG STARTUNNEL_SOURCE_REVISION=""
RUN python /tmp/stamp_client.py /app/cli/star_tunnel.py \
    --version "${STARTUNNEL_CLIENT_VERSION}" \
    --source-revision "${STARTUNNEL_SOURCE_REVISION}" \
    --server-build /app/src/core/build.py

ENV STARTUNNEL_ENV=build

# These fixed values validate production settings during static collection.
# They are not runtime credentials and do not enter the runtime image.
RUN DJANGO_SETTINGS_MODULE=startunnel.settings.production \
    STARTUNNEL_SECRET_KEY=build-only-secret-key-that-is-never-used-at-runtime-000000000000 \
    STARTUNNEL_API_KEY_PEPPER=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA \
    STARTUNNEL_IDEMPOTENCY_SECRET=CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC \
    STARTUNNEL_METRICS_TOKEN=FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF \
    STARTUNNEL_ALLOWED_HOSTS=build.invalid \
    STARTUNNEL_CSRF_TRUSTED_ORIGINS=https://build.invalid \
    STARTUNNEL_BASE_URL=https://build.invalid \
    DATABASE_URL=postgresql://build:build@127.0.0.1:5432/build \
    .venv/bin/python manage.py collectstatic --noinput

# A small host-side load runner. It has only production Python dependencies and
# the source required to provision fixtures and run the load profile.
FROM python-base AS load-helper

COPY --from=production-dependencies /app/.venv /app/.venv

ARG APP_UID=10001
ARG APP_GID=10001

RUN addgroup -g "${APP_GID}" loadhelper \
    && adduser -D -u "${APP_UID}" -G loadhelper -h /home/loadhelper loadhelper

ARG STARTUNNEL_SOURCE_REVISION="unknown"
LABEL net.startunnel.source="${STARTUNNEL_SOURCE_REVISION}"

COPY manage.py ./manage.py
COPY src ./src
COPY tests/__init__.py ./tests/__init__.py
COPY tests/load ./tests/load
COPY scripts/__init__.py scripts/compose_isolation.py scripts/start_production.py scripts/proof_timing.py scripts/load_diagnostic.py scripts/proof_build.py scripts/json_data.py ./scripts/

ENV PYTHONPATH=/app/src

RUN test -x /app/.venv/bin/python \
    && ! command -v pdm \
    && ! command -v node \
    && ! command -v perfect-doc \
    && /app/.venv/bin/python -c "import importlib.util; assert all(importlib.util.find_spec(name) is None for name in ('pytest', 'ruff', 'mypy', 'playwright', 'openapi_spec_validator'))" \
    && /app/.venv/bin/python -c "import tests.load.run_profile" \
    && chown -R loadhelper:loadhelper /app /home/loadhelper

USER loadhelper:loadhelper

FROM python-base AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

RUN apk add --no-cache ca-certificates tini \
    && ln -s /sbin/tini /usr/bin/tini \
    && addgroup -g "${APP_GID}" startunnel \
    && adduser -D -u "${APP_UID}" -G startunnel -h /home/startunnel startunnel

COPY --from=production-dependencies /app/.venv /app/.venv
COPY --from=application-builder /app/manage.py /app/manage.py
COPY --from=application-builder /app/src /app/src
COPY --from=application-builder /app/docs /app/docs
COPY --from=application-builder /app/examples /app/examples
COPY --from=application-builder /app/cli /app/cli
COPY --from=application-builder /app/generated /app/generated
COPY --from=application-builder /app/static /app/static
COPY --from=application-builder /app/staticfiles /app/staticfiles
COPY --from=application-builder /app/templates /app/templates
COPY --chmod=0555 docker/app/entrypoint.sh /usr/local/bin/startunnel-entrypoint
COPY --chmod=0555 docker/app/maintenance-healthcheck.sh /usr/local/bin/startunnel-maintenance-healthcheck

RUN rm -rf \
        /usr/local/bin/pip \
        /usr/local/bin/pip3 \
        /usr/local/bin/pip3.15 \
        /usr/local/lib/python3.15/site-packages/pip \
        /usr/local/lib/python3.15/site-packages/pip-*.dist-info \
        /app/.venv/bin/pip \
        /app/.venv/bin/pip3 \
        /app/.venv/bin/pip3.15 \
        /app/.venv/lib/python3.15/site-packages/pip \
        /app/.venv/lib/python3.15/site-packages/pip-*.dist-info \
    && chown -R startunnel:startunnel /app /home/startunnel

ENV HOME=/home/startunnel \
    PYTHONPATH=/app/src \
    STARTUNNEL_STATIC_ROOT=/app/staticfiles

USER startunnel:startunnel

EXPOSE 8000
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/startunnel-entrypoint"]
CMD ["/app/.venv/bin/uvicorn", "startunnel.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers", "--no-access-log", "--timeout-graceful-shutdown", "30"]

FROM runtime AS test-runtime

# The production runtime contains no test identity provider or adapter.
COPY tests/__init__.py tests/browser_settings.py /app/tests/

FROM development-dependencies AS development-runtime

COPY --from=documentation-tools /opt/perfect-doc/bin/perfect-doc /usr/local/bin/perfect-doc

ARG APP_UID=10001
ARG APP_GID=10001

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl nodejs npm tini postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" startunnel \
    && useradd --uid "${APP_UID}" --gid startunnel --create-home --home-dir /home/startunnel startunnel

COPY --chmod=0555 docker/app/entrypoint.sh /usr/local/bin/startunnel-entrypoint
COPY --chmod=0555 docker/app/maintenance-healthcheck.sh /usr/local/bin/startunnel-maintenance-healthcheck

ENV HOME=/home/startunnel \
    PYTHONPATH=/app/src

EXPOSE 8000
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/startunnel-entrypoint"]

FROM development-runtime AS development

COPY . .
RUN chown -R startunnel:startunnel /app /home/startunnel

USER startunnel:startunnel
CMD ["/app/.venv/bin/uvicorn", "startunnel.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers", "--no-access-log", "--timeout-graceful-shutdown", "30"]

FROM development-runtime AS browser-tests

USER root
RUN .venv/bin/playwright install --with-deps chromium firefox webkit \
    && chown -R startunnel:startunnel /home/startunnel/.cache \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN chown -R startunnel:startunnel /app /home/startunnel

USER startunnel:startunnel

# The production proxy proof uses Chromium and no documentation or Node tools.
FROM development-dependencies AS proxy-tests

ARG APP_UID=10001
ARG APP_GID=10001

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates tini \
    && groupadd --gid "${APP_GID}" startunnel \
    && useradd --uid "${APP_UID}" --gid startunnel --create-home --home-dir /home/startunnel startunnel

ENV HOME=/home/startunnel \
    PYTHONPATH=/app/src

RUN .venv/bin/playwright install --with-deps chromium \
    && chown -R startunnel:startunnel /home/startunnel/.cache \
    && rm -rf /var/lib/apt/lists/*

COPY . .
COPY --chmod=0555 docker/app/entrypoint.sh /usr/local/bin/startunnel-entrypoint
RUN chown -R startunnel:startunnel /app /home/startunnel

USER startunnel:startunnel
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/startunnel-entrypoint"]

FROM agent-dependencies AS agent-tests

ARG APP_UID=10001
ARG APP_GID=10001

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" startunnel \
    && useradd --uid "${APP_UID}" --gid startunnel --create-home --home-dir /home/startunnel startunnel

COPY . .
COPY --chmod=0555 docker/app/entrypoint.sh /usr/local/bin/startunnel-entrypoint
RUN chown -R startunnel:startunnel /app /home/startunnel

ENV HOME=/home/startunnel \
    PYTHONPATH=/app/src

USER startunnel:startunnel
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/startunnel-entrypoint"]
