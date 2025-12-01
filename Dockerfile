# BlueK9 Client - Dockerfile
# Bluetooth Surveillance System with Cyber Tools Arsenal

FROM python:3.11-slim-bookworm

LABEL maintainer="BlueK9 Team"
LABEL description="BlueK9 Bluetooth Surveillance Client with Cyber Tools"
LABEL version="2.0"

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
    net-tools \
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
    cmake \
    libssl-dev \
    libffi-dev \
    python3-dev \
    # Utilities
    curl \
    wget \
    git \
    dbus \
    udev \
    rfkill \
    # Cyber tools dependencies
    libpcap-dev \
    libusb-1.0-0-dev \
    libglib2.0-dev \
    tshark \
    tcpdump \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js for BTLEJuice
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create tools directory
RUN mkdir -p /opt/bluetooth-arsenal

# Install Ubertooth tools (from source for latest)
RUN cd /opt/bluetooth-arsenal && \
    git clone --depth 1 https://github.com/greatscottgadgets/ubertooth.git && \
    cd ubertooth/host && \
    mkdir build && cd build && \
    cmake .. && make && make install && \
    ldconfig

# Install BlueToolkit (43 exploits)
RUN cd /opt/bluetooth-arsenal && \
    git clone --depth 1 https://github.com/AetherBlack/BlueToolkit.git && \
    cd BlueToolkit && \
    pip3 install --no-cache-dir -r requirements.txt 2>/dev/null || true && \
    chmod +x *.py 2>/dev/null || true

# Install Blue Hydra (passive discovery)
RUN cd /opt/bluetooth-arsenal && \
    git clone --depth 1 https://github.com/ZeroChaos-/blue_hydra.git && \
    cd blue_hydra && \
    pip3 install --no-cache-dir -r requirements.txt 2>/dev/null || true

# Install BlueBorne scanner
RUN cd /opt/bluetooth-arsenal && \
    git clone --depth 1 https://github.com/ArmisLabs/blueborne.git 2>/dev/null || \
    git clone --depth 1 https://github.com/ArmySick/BlueBorne.git blueborne 2>/dev/null || true

# Install GATTacker (BLE MITM)
RUN cd /opt/bluetooth-arsenal && \
    git clone --depth 1 https://github.com/securing/gattacker.git && \
    cd gattacker && npm install 2>/dev/null || true

# Install Frankenstein (firmware emulator)
RUN cd /opt/bluetooth-arsenal && \
    git clone --depth 1 https://github.com/seemoo-lab/frankenstein.git 2>/dev/null || true

# Install Uberducky (HID attacks)
RUN cd /opt/bluetooth-arsenal && \
    git clone --depth 1 https://github.com/mikeryan/uberducky.git 2>/dev/null || true

# Install Python-based cyber tools
RUN pip3 install --no-cache-dir \
    blesuite \
    bleah \
    btlejack \
    btproxy \
    internalblue \
    pybluez \
    bleak \
    2>/dev/null || true

# Install BTLEJuice (BLE MITM proxy)
RUN npm install -g btlejuice 2>/dev/null || true

# Create symlinks for tools
RUN ln -sf /opt/bluetooth-arsenal/BlueToolkit /opt/BlueToolkit && \
    ln -sf /opt/bluetooth-arsenal/blue_hydra /opt/blue_hydra && \
    ln -sf /opt/bluetooth-arsenal/blueborne /opt/blueborne

# Set PATH for tools
ENV PATH="/opt/bluetooth-arsenal/BlueToolkit:/opt/bluetooth-arsenal/ubertooth/host/build:$PATH"

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
