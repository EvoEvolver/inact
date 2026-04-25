FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# ── System packages ──────────────────────────────────────────────────────────
# fuse3: required for rclone mount
# curl/ca-certificates: needed by rclone + code-server install scripts
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates fuse3 bash unzip nginx \
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
COPY workspace/start.sh .
RUN chmod +x start.sh

ENV DATA_DIR=/data

# nginx (public) / inact (internal 5051) / SMTP / code-server
EXPOSE 5050
EXPOSE 2525

# NOTE: rclone mount requires FUSE. Run with:
#   --device /dev/fuse --cap-add SYS_ADMIN
# or enable privileged mode on Railway/Render.

# nginx forwards to gunicorn on :5051; code-server on :${CODE_SERVER_PORT}
CMD ["./start.sh"]
