FROM python:3.12-slim

# System tools + Node.js 24
#
# Kept in lockstep with the `node:24-alpine` frontend-builder stage in the
# production Dockerfile. The runtime image ships no node at all, so node is a
# build-time dependency only — but a devcontainer on a different major is a
# devcontainer that can green-light a bundle the release build cannot produce.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    sudo \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# python:3.12-slim still ships pip 25.0.1, which carries five advisories fixed
# across 25.3–26.1.2. Both copies are upgraded, and they are genuinely two
# copies: `python -m venv` seeds pip from CPython's bundled ensurepip wheel, not
# from the interpreter's installed pip, so upgrading the system one does not
# reach the venv. The venv pip is the one that matters (ENV PATH below puts it
# first, so it is what init.sh and every `docker compose exec` resolve), but a
# login shell re-reads /etc/profile and lands back on /usr/local/bin/pip — so
# leaving that one stale just hides the problem behind which shell you used.
RUN pip install --no-cache-dir --upgrade "pip>=26.2.1"

# Isolated Python venv — lives inside the container, not in the mounted workspace
RUN python -m venv /opt/venv
RUN /opt/venv/bin/pip install --no-cache-dir --upgrade "pip>=26.2.1"
ENV PATH="/opt/venv/bin:$PATH"

# Make the workspace the default working directory
WORKDIR /workspace
