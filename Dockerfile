# syntax=docker/dockerfile:1.12

ARG PYTHON_BASE_IMAGE="python:3.14.7-slim-trixie@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d"

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

RUN apt-get update \
    && apt-get install --no-install-recommends --only-upgrade --yes openssl libssl3t64 \
    && rm -rf /var/lib/apt/lists/*

FROM python-base AS dependency-builder

ARG PDM_VERSION="2.28.0"

RUN python -m pip install --no-cache-dir "pdm==${PDM_VERSION}"

COPY pyproject.toml pdm.lock ./

FROM dependency-builder AS production-dependencies

RUN pdm install --prod --frozen-lockfile --no-editable \
    && find .venv -type d -name __pycache__ -prune -exec rm -rf '{}' +

FROM dependency-builder AS development-dependencies

RUN pdm install --frozen-lockfile --no-editable -G dev

FROM dependency-builder AS agent-dependencies

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

ENV STARTUNNEL_ENV=build

# These fixed values validate production settings during static collection.
# They are not runtime credentials and do not enter the runtime image.
RUN DJANGO_SETTINGS_MODULE=startunnel.settings.production \
    STARTUNNEL_SECRET_KEY=build-only-secret-key-that-is-never-used-at-runtime-000000000000 \
    STARTUNNEL_API_KEY_PEPPER=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA \
    STARTUNNEL_ADDRESS_SECRET=BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB \
    STARTUNNEL_ADDRESS_DERIVATION_SECRET=DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD \
    STARTUNNEL_IDEMPOTENCY_SECRET=CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC \
    STARTUNNEL_METRICS_TOKEN=FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF \
    STARTUNNEL_ALLOWED_HOSTS=build.invalid \
    STARTUNNEL_CSRF_TRUSTED_ORIGINS=https://build.invalid \
    STARTUNNEL_BASE_URL=https://build.invalid \
    DATABASE_URL=postgresql://build:build@127.0.0.1:5432/build \
    .venv/bin/python manage.py collectstatic --noinput

FROM python-base AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates tini \
    && apt-get purge --yes --allow-remove-essential perl-base \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" startunnel \
    && useradd --uid "${APP_UID}" --gid startunnel --create-home --home-dir /home/startunnel startunnel

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
        /usr/local/bin/pip3.14 \
        /usr/local/lib/python3.14/site-packages/pip \
        /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
        /app/.venv/bin/pip \
        /app/.venv/bin/pip3 \
        /app/.venv/bin/pip3.14 \
        /app/.venv/lib/python3.14/site-packages/pip \
        /app/.venv/lib/python3.14/site-packages/pip-*.dist-info \
    && chown -R startunnel:startunnel /app /home/startunnel

ENV HOME=/home/startunnel \
    PYTHONPATH=/app/src \
    STARTUNNEL_STATIC_ROOT=/app/staticfiles

USER startunnel:startunnel

EXPOSE 8000
STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/startunnel-entrypoint"]
CMD ["/app/.venv/bin/uvicorn", "startunnel.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--no-proxy-headers", "--no-access-log", "--timeout-graceful-shutdown", "30"]

FROM runtime AS test-runtime

# The production runtime contains no test identity provider or adapter.
COPY tests/__init__.py tests/browser_settings.py /app/tests/

FROM development-dependencies AS development-runtime

ARG APP_UID=10001
ARG APP_GID=10001

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl nodejs npm tini \
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
CMD ["/app/.venv/bin/uvicorn", "startunnel.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--no-proxy-headers", "--no-access-log", "--timeout-graceful-shutdown", "30"]

FROM development-runtime AS browser-tests

USER root
RUN .venv/bin/playwright install --with-deps chromium firefox webkit \
    && chown -R startunnel:startunnel /home/startunnel/.cache \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN chown -R startunnel:startunnel /app /home/startunnel

USER startunnel:startunnel

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
