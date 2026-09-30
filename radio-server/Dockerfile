FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Set working directory
WORKDIR /app

# Copy uv configuration
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen

# Copy application code
COPY src/ ./src/
COPY run.py ./
COPY test_*.py ./

# Create necessary directories
RUN mkdir -p /app/temp-audio /app/logs /app/audio-output

# Start the radio server
CMD ["uv", "run", "python", "run.py"]
