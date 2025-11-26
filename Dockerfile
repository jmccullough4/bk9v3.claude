# BlueK9 Client - Dockerfile
# Bluetooth Surveillance System

FROM python:3.11-slim-bookworm

LABEL maintainer="BlueK9 Team"
LABEL description="BlueK9 Bluetooth Surveillance Client"
LABEL version="1.0"

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Bluetooth tools
    bluetooth \
    bluez \
    bluez-tools \
    libbluetooth-dev \
    # Network tools
    iproute2 \
    wireless-tools \
    iw \
    # GPS tools
    gpsd \
    gpsd-clients \
    # Modem tools for SMS
    modemmanager \
    libqmi-utils \
    # Audio tools
    alsa-utils \
    pulseaudio \
    # Build tools
    build-essential \
    pkg-config \
    # Utilities
    curl \
    wget \
    dbus \
    udev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements first for caching
COPY client/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY client/ ./client/
COPY scripts/ ./scripts/
COPY logs/ ./logs/

# Create necessary directories
RUN mkdir -p /app/logs /app/data

# Download alert sound during build
RUN mkdir -p /app/client/static/sounds && \
    wget -q -O /app/client/static/sounds/alert.mp3 \
    "https://github.com/freeCodeCamp/cdn/raw/main/build/testable-projects-fcc/audio/BeepSound.wav" || \
    echo "Alert sound download skipped - will use fallback"

# Set working directory to client
WORKDIR /app/client

# Expose port
EXPOSE 5000

# Environment variables
ENV FLASK_APP=app.py
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/login || exit 1

# Run the application
CMD ["python", "app.py"]
