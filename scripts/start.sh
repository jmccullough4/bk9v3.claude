#!/bin/bash
#
# BlueK9 Client Start Script
# Bluetooth Surveillance System
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Get script directory
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

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# GitHub repository for updates
GITHUB_REPO="your-org/bluek9"  # Change this to actual repository
VERSION_FILE="$PROJECT_DIR/VERSION"
CURRENT_VERSION="0.0.0"

# Check for updates from GitHub
check_updates() {
    log_info "Checking for updates..."

    # Get current version
    if [ -f "$VERSION_FILE" ]; then
        CURRENT_VERSION=$(cat "$VERSION_FILE" | tr -d '[:space:]')
    elif command -v git &> /dev/null && [ -d "$PROJECT_DIR/.git" ]; then
        CURRENT_VERSION=$(git -C "$PROJECT_DIR" describe --tags --always 2>/dev/null || echo "unknown")
    fi

    log_info "Current version: $CURRENT_VERSION"

    # Try to get latest version from GitHub (requires curl and internet)
    if ! command -v curl &> /dev/null; then
        log_warn "curl not found, skipping update check"
        return 0
    fi

    # Try to fetch latest release info from GitHub API (times out after 5 seconds)
    LATEST_INFO=$(curl -s --connect-timeout 5 -m 10 \
        "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" 2>/dev/null) || {
        log_warn "Could not check for updates (offline or repository not found)"
        return 0
    }

    # Parse version from response
    LATEST_VERSION=$(echo "$LATEST_INFO" | grep -o '"tag_name": *"[^"]*"' | cut -d'"' -f4)

    if [ -z "$LATEST_VERSION" ]; then
        log_warn "Could not determine latest version"
        return 0
    fi

    log_info "Latest version: $LATEST_VERSION"

    # Compare versions (simple string comparison - works for semver)
    if [ "$CURRENT_VERSION" != "$LATEST_VERSION" ] && [ "$CURRENT_VERSION" != "unknown" ]; then
        echo ""
        echo -e "${YELLOW}================================================"
        echo "  UPDATE AVAILABLE!"
        echo "  Current: $CURRENT_VERSION"
        echo "  Latest:  $LATEST_VERSION"
        echo ""
        echo "  To update, run:"
        echo "    cd $PROJECT_DIR && git pull"
        echo "  Or download from:"
        echo "    https://github.com/${GITHUB_REPO}/releases/latest"
        echo "================================================${NC}"
        echo ""

        # Ask if user wants to continue
        read -t 10 -p "Continue with current version? [Y/n] " -n 1 -r REPLY || REPLY="Y"
        echo ""
        if [[ $REPLY =~ ^[Nn]$ ]]; then
            log_info "Exiting. Please update and restart."
            exit 0
        fi
    else
        log_info "You are running the latest version"
    fi
}

# Check root
check_root() {
    if [[ $EUID -ne 0 ]]; then
        log_error "This script must be run as root for Bluetooth access"
        echo "Try: sudo $0"
        exit 1
    fi
}

# Check dependencies
check_dependencies() {
    log_info "Checking dependencies..."

    # Check Python
    if ! command -v python3 &> /dev/null; then
        log_error "Python3 not found. Run install.sh first."
        exit 1
    fi

    # Check virtual environment
    if [ ! -d "$CLIENT_DIR/venv" ]; then
        log_error "Virtual environment not found. Run install.sh first."
        exit 1
    fi

    # Check Bluetooth
    if ! command -v hciconfig &> /dev/null; then
        log_error "BlueZ tools not found. Run install.sh first."
        exit 1
    fi
}

# Start Bluetooth service
start_bluetooth() {
    log_info "Starting Bluetooth service..."

    systemctl start bluetooth 2>/dev/null || true

    # Unblock Bluetooth
    rfkill unblock bluetooth 2>/dev/null || true

    # Bring up default adapter
    hciconfig hci0 up 2>/dev/null || log_warn "Could not bring up hci0"

    # List available adapters
    echo ""
    log_info "Available Bluetooth adapters:"
    hciconfig -a 2>/dev/null | grep -E "^hci|BD Address" || log_warn "No adapters found"
    echo ""
}

# Start GPS service
start_gps() {
    log_info "Starting GPS service..."
    systemctl start gpsd 2>/dev/null || log_warn "GPS service not available"
}

# Start ModemManager
start_modem() {
    log_info "Starting ModemManager for SMS..."
    systemctl start ModemManager 2>/dev/null || log_warn "ModemManager not available"
}

# Create logs directory
setup_logs() {
    mkdir -p "$PROJECT_DIR/logs"
}

# Start the application
start_app() {
    log_info "Starting BlueK9 Client..."
    echo ""
    echo -e "${CYAN}================================================"
    echo "  BlueK9 is starting..."
    echo "  Access: http://localhost:5000"
    echo "  Login:  bluek9 / warhammer"
    echo "  Press Ctrl+C to stop"
    echo "================================================${NC}"
    echo ""

    cd "$CLIENT_DIR"
    source venv/bin/activate

    # Start with proper permissions
    python app.py
}

# Docker start
start_docker() {
    log_info "Starting BlueK9 with Docker..."
    cd "$PROJECT_DIR"

    if ! command -v docker &> /dev/null; then
        log_error "Docker not installed. Use: sudo ./scripts/install.sh"
        exit 1
    fi

    docker-compose up -d
    log_info "BlueK9 started in Docker container"
    log_info "Access: http://localhost:5000"
    log_info "Logs: docker-compose logs -f"
}

# Show help
show_help() {
    echo "BlueK9 Start Script"
    echo ""
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --docker      Start using Docker"
    echo "  --no-update   Skip update check"
    echo "  --help        Show this help message"
    echo ""
}

# Main
main() {
    print_banner

    SKIP_UPDATE=false

    # Parse arguments
    for arg in "$@"; do
        case "$arg" in
            --docker)
                check_root
                start_docker
                exit 0
                ;;
            --no-update)
                SKIP_UPDATE=true
                ;;
            --help|-h)
                show_help
                exit 0
                ;;
        esac
    done

    check_root

    # Check for updates (unless skipped)
    if [ "$SKIP_UPDATE" = false ]; then
        check_updates
    fi

    check_dependencies
    setup_logs
    start_bluetooth
    start_gps
    start_modem
    start_app
}

# Handle Ctrl+C
trap 'echo ""; log_info "Shutting down BlueK9..."; exit 0' INT TERM

main "$@"
