FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Install Python dependencies
COPY agent/pyproject.toml .
RUN python3 -c \
    "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); open('requirements.txt','w').write('\n'.join(d['project']['dependencies']))" && \
    uv pip install --system --no-cache -r requirements.txt && \
    rm requirements.txt

# System packages + rclone
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends curl ca-certificates unzip fuse3 && \
    curl -fsSL https://rclone.org/install.sh | bash && \
    echo "user_allow_other" >> /etc/fuse.conf && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Node.js + Claude CLI + Codex CLI
RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    npm install -g @anthropic-ai/claude-code @openai/codex && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy agent source
COPY agent/agent.py .
COPY agent/memory.py .
COPY agent/server.py .

# Callback server port
EXPOSE 7779

# Environment variables — set these in Railway / docker run -e
# SERVER, AGENT_KEY, AGENT_ID, CALLBACK_URL, PORT, REVIVAL_INTERVAL, ALLOWED_TOOLS
# ANTHROPIC_API_KEY — required by claude CLI
# OPENAI_API_KEY    — required by codex CLI
# OPENAI_BASE_URL   — optional: point to OpenRouter (https://openrouter.ai/api/v1)
# MEMORY_DIR        — mount a Railway volume here for persistent memory

ENV PORT=7779
ENV MEMORY_DIR=/memory

CMD ["python", "server.py"]
