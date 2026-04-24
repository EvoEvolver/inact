FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Install workspace dependencies from pyproject.toml
COPY workspace/pyproject.toml .
RUN python3 -c \
    "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); open('requirements.txt','w').write('\n'.join(d['project']['dependencies']))" && \
    uv pip install --system --no-cache -r requirements.txt && \
    rm requirements.txt

# Install inact from source (not yet on PyPI)
COPY inact/ ./inact/
COPY pyproject.toml ./inact_pyproject.toml
RUN uv pip install --system --no-cache ./inact/.. --no-deps

# Copy workspace server
COPY workspace/server.py .

# Data volume — mount a host directory here for persistence
ENV DATA_DIR=/data
VOLUME /data

# HTTP server port (default 5050, override with -e PORT=...)
EXPOSE 5050
# Embedded SMTP server port — port-forward 25 → this on the host
EXPOSE 2525

# One worker only: the embedded aiosmtpd SMTP server cannot be shared
# across multiple processes. Scale horizontally with separate instances
# behind a load balancer (each on its own SMTP port).
CMD exec gunicorn server:wsgi \
        --bind "0.0.0.0:${PORT:-5050}" \
        --workers 1 \
        --timeout 120 \
        --access-logfile - \
        --error-logfile -
