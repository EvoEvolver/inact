FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# ── System packages ──────────────────────────────────────────────────────────
# fuse3: required for rclone mount
# curl/ca-certificates: needed by rclone + code-server install scripts
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates fuse3 bash \
    && rm -rf /var/lib/apt/lists/*

# Allow non-root FUSE mounts (needed for rclone --allow-other)
RUN echo "user_allow_other" >> /etc/fuse.conf

# ── rclone ───────────────────────────────────────────────────────────────────
RUN curl -fsSL https://rclone.org/install.sh | bash

# ── code-server (VSCode in browser) ──────────────────────────────────────────
RUN curl -fsSL https://code-server.dev/install.sh | sh

# ── Python dependencies ───────────────────────────────────────────────────────
WORKDIR /app

COPY workspace/pyproject.toml .
RUN python3 -c \
    "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); open('requirements.txt','w').write('\n'.join(d['project']['dependencies']))" && \
    uv pip install --system --no-cache -r requirements.txt && \
    rm requirements.txt

# ── inact from source ─────────────────────────────────────────────────────────
COPY inact/ ./inact/
COPY pyproject.toml ./inact_pyproject.toml
RUN uv pip install --system --no-cache ./inact/.. --no-deps

# ── Workspace server ──────────────────────────────────────────────────────────
COPY workspace/server.py .

ENV DATA_DIR=/data

# inact HTTP / embedded SMTP / code-server
EXPOSE 5050
EXPOSE 2525
EXPOSE 8080

# NOTE: rclone mount requires FUSE. Run the container with:
#   --device /dev/fuse --cap-add SYS_ADMIN
# or on Railway/Render set privileged mode.

# One worker: aiosmtpd + rclone subprocesses cannot be shared across workers
CMD exec gunicorn server:wsgi \
        --bind "0.0.0.0:${PORT:-5050}" \
        --workers 1 \
        --timeout 120 \
        --access-logfile - \
        --error-logfile -
