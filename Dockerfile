# syntax disabled: syntax=docker/dockerfile:1.7
#
# Production image for billa-gates.
#
# Four stages; only artifacts from stages 1–3 land in the final image
# (no node, no pip, no build tools). Expected final image: ~165–190 MB.
#
# Build:
#   docker build --build-arg RESTIC_ARCH=arm64 -t billa-gates .
#   docker build --build-arg RESTIC_ARCH=amd64 -t billa-gates .
#
# RESTIC_ARCH is required (no default) — build fails loudly without it.

# ─── Stage 1 — frontend-builder ───────────────────────────────────────────────
FROM node:22-alpine AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --prefer-offline
COPY frontend/ ./
RUN npm run build
# Output: /frontend/dist/

# ─── Stage 2 — restic-fetcher ─────────────────────────────────────────────────
FROM alpine:3.21 AS restic-fetcher
RUN apk add --no-cache curl bzip2
ARG RESTIC_VERSION=0.18.1
ARG RESTIC_ARCH
# RESTIC_ARCH must be 'arm64' (Apple Silicon / ARM64 Linux) or 'amd64' (Intel/AMD).
# Fail the build immediately with a clear message if it's missing or wrong.
RUN if [ "$RESTIC_ARCH" != "arm64" ] && [ "$RESTIC_ARCH" != "amd64" ]; then \
      echo "ERROR: --build-arg RESTIC_ARCH must be 'arm64' or 'amd64' (got '${RESTIC_ARCH}')"; \
      exit 1; \
    fi
WORKDIR /tmp/restic
# Download into the original filename so SHA256SUMS lines match by name.
RUN ARCHIVE="restic_${RESTIC_VERSION}_linux_${RESTIC_ARCH}.bz2" \
    && BINARY="restic_${RESTIC_VERSION}_linux_${RESTIC_ARCH}" \
    && curl -fsSL "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/${ARCHIVE}" -o "${ARCHIVE}" \
    && curl -fsSL "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/SHA256SUMS" -o SHA256SUMS \
    && grep " ${ARCHIVE}\$" SHA256SUMS | sha256sum -c - \
    && bunzip2 "${ARCHIVE}" \
    && chmod 755 "${BINARY}" \
    && mv "${BINARY}" /restic
# Output: /restic

# ─── Stage 3 — python-builder ─────────────────────────────────────────────────
FROM python:3.12-alpine AS python-builder
RUN apk add --no-cache build-base
RUN python -m venv /venv
COPY backend/requirements.txt /tmp/requirements.txt
RUN /venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt
# Output: /venv/

# ─── Stage 4 — runtime ────────────────────────────────────────────────────────
FROM python:3.12-alpine AS runtime
RUN apk add --no-cache ca-certificates

COPY --from=frontend-builder /frontend/dist  /app/static
COPY --from=restic-fetcher   /restic         /usr/local/bin/restic
COPY --from=python-builder   /venv           /venv
COPY backend/app             /app/app
COPY backend/alembic         /app/alembic
COPY backend/alembic.ini     /app/alembic.ini

ENV PATH="/venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1
ENV RESTIC_CACHE_DIR="/app/data/restic-cache"

WORKDIR /app
EXPOSE 12345
ENTRYPOINT ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 12345"]
