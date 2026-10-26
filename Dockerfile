FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code@2.1.310 && npm cache clean --force

# Debian bookworm only ships Python 3.11; uv (used at build time only) installs 3.12 into a venv.
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_NO_CACHE=1 PATH=/opt/venv/bin:$PATH
COPY requirements.txt /app/
RUN --mount=from=ghcr.io/astral-sh/uv:0.13.5,source=/uv,target=/bin/uv \
    uv venv --python 3.12 /opt/venv \
    && uv pip install --python /opt/venv/bin/python -r /app/requirements.txt

WORKDIR /app
COPY app.py review_prompt.md ./
USER node
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
