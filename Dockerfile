# Almali Library Curator for Plex: container image (S8).
#
# Architecture-neutral: the official Python image is published for amd64 and
# arm64, and nothing below downloads an architecture-specific binary.

# --- Build stage: a wheel from the repository's own pyproject.toml ----------
FROM python:3.12-slim AS build

WORKDIR /src
COPY pyproject.toml README.md ./
COPY almali_library_curator ./almali_library_curator
COPY docker/constraints.txt /constraints.txt
RUN pip wheel --no-cache-dir --constraint /constraints.txt --wheel-dir /wheels .

# --- Runtime stage ----------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ALC_CONFIG_DIR=/config \
    PUID=99 \
    PGID=100 \
    TZ=Etc/UTC

COPY --from=build /wheels /wheels
COPY --from=build /constraints.txt /constraints.txt
RUN pip install --no-cache-dir --no-index --find-links=/wheels \
        --constraint /constraints.txt almali-library-curator \
    && rm -rf /wheels /constraints.txt \
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
