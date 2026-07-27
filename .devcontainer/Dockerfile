FROM python:3.12-slim

# System tools + Node.js 22
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    sudo \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
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
RUN pip install --no-cache-dir --upgrade "pip>=26.1.2"

# Isolated Python venv — lives inside the container, not in the mounted workspace
RUN python -m venv /opt/venv
RUN /opt/venv/bin/pip install --no-cache-dir --upgrade "pip>=26.1.2"
ENV PATH="/opt/venv/bin:$PATH"

# Make the workspace the default working directory
WORKDIR /workspace
