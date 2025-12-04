# BlueK9 Client - Dockerfile
# Bluetooth Surveillance System - Docker Plugin Architecture
# Optimized for Ubuntu 24.04+ and Python 3.12+

FROM python:3.12-slim-bookworm

LABEL maintainer="BlueK9 Team"
LABEL description="BlueK9 Bluetooth Surveillance Client"
LABEL version="3.3"

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install system dependencies - core Bluetooth and networking tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core Bluetooth stack (BlueZ)
    bluetooth \
    bluez \
    bluez-tools \
    libbluetooth-dev \
    # Network tools
    iproute2 \
    iw \
    net-tools \
    # GPS support
    gpsd \
    gpsd-clients \
    # Modem tools for SMS alerts
    modemmanager \
    libqmi-utils \
    # D-Bus for BlueZ communication
    dbus \
    # USB device access
    udev \
    libusb-1.0-0 \
    # RF control
    rfkill \
    # Process management
    procps \
    # Minimal utilities
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create app directory structure
WORKDIR /app

# Copy requirements first for layer caching
COPY client/requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY client/ ./client/
COPY scripts/ ./scripts/

# Create necessary directories
RUN mkdir -p /app/logs /app/data /app/client/static/sounds

# Set working directory to client
WORKDIR /app/client

# Create non-root user for container (but Bluetooth requires root)
# Application will run as root for hardware access

# Expose port for web UI
EXPOSE 5000

# Environment variables
ENV FLASK_APP=app.py
ENV FLASK_ENV=production

# Health check - verify web server is responding
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5000/login || exit 1

# Default command - run the Flask application
CMD ["python", "app.py"]
