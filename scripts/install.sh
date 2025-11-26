#!/bin/bash
#
# BlueK9 Client Installation Script
# Bluetooth Surveillance System
#
# This script installs all dependencies and sets up the BlueK9 client.
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Get script directory (before any cd commands)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CLIENT_DIR="$PROJECT_DIR/client"

# ASCII Banner
print_banner() {
    echo -e "${CYAN}"
    echo "=============================================="
    echo "    ____  __    __  ________ __ _____  "
    echo "   / __ )/ /   / / / / ____/ //_/ __ \ "
    echo "  / __  / /   / / / / __/ / ,< / /_/ / "
    echo " / /_/ / /___/ /_/ / /___/ /| |\__, /  "
    echo "/_____/_____/\____/_____/_/ |_/____/   "
    echo "                                        "
    echo "   BLUETOOTH SURVEILLANCE SYSTEM        "
    echo "=============================================="
    echo -e "${NC}"
}

# Logging functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    echo -e "${BLUE}[STEP]${NC} $1"
}

# Check if running as root
check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "This script must be run as root (use sudo)"
        exit 1
    fi
}

# Detect OS
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$NAME
        VER=$VERSION_ID
    else
        log_error "Cannot detect OS. This script supports Debian/Ubuntu-based systems."
        exit 1
    fi
    log_info "Detected OS: $OS $VER"
}

# Update system
update_system() {
    log_step "Updating system packages..."
    apt-get update -qq
    apt-get upgrade -y -qq
}

# Install system dependencies
install_dependencies() {
    log_step "Installing system dependencies..."

    # Core dependencies
    apt-get install -y -qq \
        python3 \
        python3-pip \
        python3-venv \
        python3-dev \
        build-essential \
        pkg-config

    # Bluetooth packages
    log_info "Installing Bluetooth tools..."
    apt-get install -y -qq \
        bluetooth \
        bluez \
        bluez-tools \
        libbluetooth-dev \
        rfkill

    # Network tools
    log_info "Installing network tools..."
    apt-get install -y -qq \
        iproute2 \
        wireless-tools \
        iw \
        net-tools

    # GPS packages
    log_info "Installing GPS tools..."
    apt-get install -y -qq \
        gpsd \
        gpsd-clients \
        python3-gps

    # Modem Manager for SMS
    log_info "Installing ModemManager for SMS..."
    apt-get install -y -qq \
        modemmanager \
        libqmi-utils \
        libmbim-utils

    # Audio tools
    log_info "Installing audio tools..."
    apt-get install -y -qq \
        alsa-utils \
        pulseaudio || log_warn "Audio packages may require manual configuration"

    # Docker (optional)
    if ! command -v docker &> /dev/null; then
        log_info "Installing Docker..."
        curl -fsSL https://get.docker.com -o get-docker.sh
        sh get-docker.sh
        rm get-docker.sh
        usermod -aG docker $SUDO_USER 2>/dev/null || true
    else
        log_info "Docker already installed"
    fi

    # Docker Compose
    if ! command -v docker-compose &> /dev/null; then
        log_info "Installing Docker Compose..."
        apt-get install -y -qq docker-compose-plugin || \
            pip3 install docker-compose
    fi

    # Other utilities
    apt-get install -y -qq \
        curl \
        wget \
        git \
        dbus \
        udev
}

# Configure Bluetooth
configure_bluetooth() {
    log_step "Configuring Bluetooth service..."

    # Enable and start Bluetooth service
    systemctl enable bluetooth
    systemctl start bluetooth

    # Unblock Bluetooth if blocked
    rfkill unblock bluetooth 2>/dev/null || true

    # Check for Bluetooth adapters
    if hciconfig | grep -q "hci"; then
        log_info "Bluetooth adapter(s) detected:"
        hciconfig -a | grep -E "^hci|BD Address"
    else
        log_warn "No Bluetooth adapters detected. Please connect a Bluetooth adapter."
    fi
}

# Configure GPS
configure_gps() {
    log_step "Configuring GPS service..."

    # Create gpsd config
    cat > /etc/default/gpsd << 'EOF'
# Default settings for gpsd
START_DAEMON="true"
GPSD_OPTIONS="-n"
DEVICES="/dev/ttyUSB0 /dev/ttyACM0"
USBAUTO="true"
EOF

    # Enable gpsd
    systemctl enable gpsd
    systemctl start gpsd || log_warn "GPS service may need a GPS device connected"
}

# Configure ModemManager
configure_modem() {
    log_step "Configuring ModemManager for SMS alerts..."

    systemctl enable ModemManager
    systemctl start ModemManager

    # Check for modems
    if mmcli -L | grep -q "Modem"; then
        log_info "Modem(s) detected:"
        mmcli -L
    else
        log_warn "No modems detected. SMS alerts will be unavailable until a modem is connected."
    fi
}

# Install Python dependencies
install_python_deps() {
    log_step "Installing Python dependencies..."

    cd "$CLIENT_DIR"

    # Create virtual environment
    python3 -m venv venv
    source venv/bin/activate

    # Upgrade pip
    pip install --upgrade pip

    # Install requirements
    pip install -r requirements.txt

    deactivate

    log_info "Python dependencies installed"
}

# Download alert sound
download_alert_sound() {
    log_step "Downloading alert sound..."

    SOUNDS_DIR="$CLIENT_DIR/static/sounds"
    mkdir -p "$SOUNDS_DIR"

    # Download a simple alert sound
    # Using a reliable public domain source
    if ! [ -f "$SOUNDS_DIR/alert.mp3" ]; then
        wget -q -O "$SOUNDS_DIR/alert.wav" \
            "https://www.soundjay.com/buttons/sounds/beep-07a.mp3" 2>/dev/null || \
        wget -q -O "$SOUNDS_DIR/alert.wav" \
            "https://github.com/freeCodeCamp/cdn/raw/main/build/testable-projects-fcc/audio/BeepSound.wav" 2>/dev/null || \
        log_warn "Could not download alert sound. Please add an alert.mp3 or alert.wav to $SOUNDS_DIR"
    fi

    # Create a fallback beep sound using sox if available
    if command -v sox &> /dev/null && ! [ -f "$SOUNDS_DIR/alert.wav" ]; then
        sox -n "$SOUNDS_DIR/alert.wav" synth 0.3 sine 880 vol 0.5
        log_info "Generated alert sound using sox"
    fi
}

# Create directories
create_directories() {
    log_step "Creating directories..."

    mkdir -p "$PROJECT_DIR/logs"
    mkdir -p "$PROJECT_DIR/data"
    mkdir -p "$PROJECT_DIR/client/static/sounds"

    # Set permissions
    chmod -R 755 "$PROJECT_DIR"
    chown -R $SUDO_USER:$SUDO_USER "$PROJECT_DIR" 2>/dev/null || true
}

# Create systemd service
create_service() {
    log_step "Creating systemd service..."

    cat > /etc/systemd/system/bluek9.service << EOF
[Unit]
Description=BlueK9 Bluetooth Surveillance Client
After=network.target bluetooth.target
Wants=bluetooth.target

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_DIR/client
ExecStart=$PROJECT_DIR/client/venv/bin/python app.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    log_info "Service created. Use 'sudo systemctl start bluek9' to start"
}

# Print completion message
print_completion() {
    echo ""
    echo -e "${GREEN}=============================================="
    echo "  INSTALLATION COMPLETE!"
    echo "==============================================${NC}"
    echo ""
    echo -e "${CYAN}To start BlueK9:${NC}"
    echo ""
    echo "  Option 1 - Direct:"
    echo "    cd $PROJECT_DIR/client"
    echo "    source venv/bin/activate"
    echo "    sudo python app.py"
    echo ""
    echo "  Option 2 - Using start script:"
    echo "    sudo $PROJECT_DIR/scripts/start.sh"
    echo ""
    echo "  Option 3 - Using Docker:"
    echo "    cd $PROJECT_DIR"
    echo "    sudo docker-compose up -d"
    echo ""
    echo "  Option 4 - Using systemd service:"
    echo "    sudo systemctl start bluek9"
    echo "    sudo systemctl enable bluek9  # Start on boot"
    echo ""
    echo -e "${CYAN}Access the web interface:${NC}"
    echo "    http://localhost:5000"
    echo ""
    echo -e "${CYAN}Login credentials:${NC}"
    echo "    Username: bluek9"
    echo "    Password: warhammer"
    echo ""
    echo -e "${YELLOW}Important Notes:${NC}"
    echo "  - Run with sudo for Bluetooth hardware access"
    echo "  - Connect GPS device for location tracking"
    echo "  - Connect SIMCOM7600 for SMS alerts"
    echo "  - Primary Bluetooth radio: Sena UD100"
    echo ""
}

# Main installation
main() {
    print_banner
    check_root
    detect_os
    update_system
    install_dependencies
    configure_bluetooth
    configure_gps
    configure_modem
    create_directories
    install_python_deps
    download_alert_sound
    create_service
    print_completion
}

# Run main
main "$@"
