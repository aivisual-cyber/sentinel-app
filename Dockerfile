# Sentinel Fire Detection System - Production Edge Container
FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04

# Prevent interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    libgl1 \
    libglib2.0-0 \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency definition
COPY requirements.txt .

# Install Python requirements
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY backend/ backend/
COPY config/ config/
COPY scripts/ scripts/
COPY run_sentinel.py rabahdev_best.pt ./

ENV SENTINEL_HOME=/app SENTINEL_PORT=8765

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python3 -c "import json,sys,urllib.request; s=json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4))['status']; sys.exit(1 if s == 'critical' else 0)" || exit 1

EXPOSE 8765

CMD ["python3", "run_sentinel.py"]
