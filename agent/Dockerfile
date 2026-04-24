FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Install Python dependencies
COPY agent/pyproject.toml .
RUN python3 -c \
    "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); open('requirements.txt','w').write('\n'.join(d['project']['dependencies']))" && \
    uv pip install --system --no-cache -r requirements.txt && \
    rm requirements.txt

# Install Claude CLI (requires Node.js)
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g @anthropic-ai/claude-code && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy agent source
COPY agent/agent.py .

# Callback server port
EXPOSE 7779

# Environment variables — set these in Railway / docker run -e
# SERVER, AGENT_KEY, AGENT_ID, CALLBACK_URL, PORT, REVIVAL_INTERVAL, ALLOWED_TOOLS
# ANTHROPIC_API_KEY — required by claude CLI
# MEMORY_DIR        — mount a Railway volume here for persistent memory

ENV PORT=7779
ENV MEMORY_DIR=/memory

CMD ["python", "agent.py"]
