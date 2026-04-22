FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev && rm -rf /var/lib/apt/lists/*

# Install Python deps (docker-specific: excludes offline ML tools like sentence-transformers/torch)
COPY requirements-docker.txt .
RUN pip install --no-cache-dir -r requirements-docker.txt

# Copy source
COPY . .

# API port
EXPOSE 8080

# Default: dry-run mode
CMD ["python", "-m", "arbiter"]
