# Almali Library Curator for Plex: container image (S8).
#
# Architecture-neutral: the official Python image is published for amd64 and
# arm64, and nothing below downloads an architecture-specific binary.
#
# The third-party dependencies are installed from the committed, hash-pinned
# locks in docker/ (T8, S19): pip runs with --require-hashes, so the build
# fails when a requirement carries no hash or when the downloaded artifact does
# not match the lock. Each lock hashes every distribution of its pinned
# versions, so the same lock builds the image on both published architectures.

# --- Build stage: a wheel from the repository's own pyproject.toml ----------
# #71: pinned by the manifest digest, not the moving tag; Dependabot's
# docker ecosystem keeps the digest current on every base-image update.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build

WORKDIR /src
COPY pyproject.toml README.md ./
COPY almali_library_curator ./almali_library_curator
COPY docker/requirements.lock docker/build-requirements.lock /

# The build toolchain comes from the hashed lock as well: the backend never
# downloads its own dependencies, and the dependencies of the package are
# fetched and verified before the wheel is built.
RUN pip install --no-cache-dir --require-hashes -r /build-requirements.lock \
    && pip wheel --no-cache-dir --require-hashes --only-binary=:all: \
        --wheel-dir /wheels -r /requirements.lock \
    && pip wheel --no-cache-dir --no-build-isolation --no-deps --wheel-dir /wheels .

# --- Runtime stage ----------------------------------------------------------
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ALC_CONFIG_DIR=/config \
    PUID=99 \
    PGID=100 \
    TZ=Etc/UTC

COPY --from=build /wheels /wheels
COPY docker/requirements.lock /requirements.lock
RUN pip install --no-cache-dir --no-index --find-links=/wheels --require-hashes -r /requirements.lock \
    && pip install --no-cache-dir --no-index --find-links=/wheels --no-deps almali-library-curator \
    && rm -rf /wheels /requirements.lock \
    && mkdir -p /config

# Plain COPY + chmod: works with BuildKit and with the legacy builder.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/healthcheck.py /usr/local/lib/alc/healthcheck.py
RUN chmod 0755 /usr/local/bin/entrypoint.sh \
    && chmod 0644 /usr/local/lib/alc/healthcheck.py

VOLUME ["/config"]
EXPOSE 17540

# 200 with "status": "ok" is healthy even before Plex is connected (L98 d).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s --retries=3 \
    CMD ["python", "/usr/local/lib/alc/healthcheck.py"]

# The entrypoint starts as root only to fix /config ownership, then drops to
# PUID:PGID before the app starts.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# One worker keeps the scan scheduler and lifecycle writes in one process;
# threads serve concurrent page requests.
CMD ["gunicorn", "--bind", "0.0.0.0:17540", "--workers", "1", "--threads", "4", "--access-logfile", "-", "almali_library_curator.app:create_app()"]
