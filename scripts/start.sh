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

# BlueK9 installation directory for auto-update
BLUEK9_INSTALL_DIR="/apps/bk9v3.claude"

# Ensure proper environment for systemd/cron execution
# Git requires HOME to find credentials and config
export HOME="${HOME:-/root}"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

# Auto-update from GitHub
auto_update() {
    log_info "Checking for updates from GitHub..."

    # Check if install directory exists and is a git repo
    if [ ! -d "$BLUEK9_INSTALL_DIR/.git" ]; then
        log_warn "BlueK9 not installed as git repo at $BLUEK9_INSTALL_DIR, skipping auto-update"
        return 0
    fi

    cd "$BLUEK9_INSTALL_DIR" || {
        log_warn "Could not change to $BLUEK9_INSTALL_DIR"
        return 0
    }

    # Get current commit hash
    CURRENT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
    log_info "Current version: $CURRENT_COMMIT"

    # Check for network connectivity with timeout
    if ! git ls-remote --exit-code origin HEAD &>/dev/null; then
        log_warn "Cannot reach GitHub, continuing with current version"
        cd - > /dev/null
        return 0
    fi

    # Fetch latest changes
    log_info "Fetching latest changes from GitHub..."
    if git fetch origin main --quiet 2>/dev/null || git fetch origin master --quiet 2>/dev/null; then
        # Check if we're behind
        LOCAL=$(git rev-parse HEAD)
        REMOTE=$(git rev-parse @{u} 2>/dev/null || git rev-parse origin/main 2>/dev/null || git rev-parse origin/master 2>/dev/null)

        if [ "$LOCAL" != "$REMOTE" ]; then
            echo ""
            echo -e "${CYAN}================================================"
            echo "  UPDATE AVAILABLE!"
            echo "  Applying updates from GitHub..."
            echo "================================================${NC}"
            echo ""

            # Stash any local changes
            git stash --quiet 2>/dev/null

            # Pull latest changes
            if git pull origin main --quiet 2>/dev/null || git pull origin master --quiet 2>/dev/null; then
                NEW_COMMIT=$(git rev-parse --short HEAD)
                log_info "Updated successfully: $CURRENT_COMMIT -> $NEW_COMMIT"

                # Show recent changes
                echo ""
                echo -e "${GREEN}Recent changes:${NC}"
                git log --oneline -5 2>/dev/null || true
                echo ""

                # Check if requirements changed
                if git diff --name-only "$CURRENT_COMMIT" HEAD 2>/dev/null | grep -q "requirements.txt"; then
                    log_warn "requirements.txt changed - updating dependencies..."
                    if [ -d "$CLIENT_DIR/venv" ]; then
                        source "$CLIENT_DIR/venv/bin/activate"
                        pip install -r "$CLIENT_DIR/requirements.txt" --quiet 2>/dev/null || log_warn "Failed to update dependencies"
                    fi
                fi
            else
                log_warn "Failed to pull updates, continuing with current version"
                git stash pop --quiet 2>/dev/null || true
            fi
        else
            log_info "Already running the latest version"
        fi
    else
        log_warn "Could not fetch from GitHub, continuing with current version"
    fi

    cd - > /dev/null
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

    # Bring up all available adapters (hci0=UD100, hci1=AX210)
    log_info "Bringing up Bluetooth adapters..."
    hciconfig hci0 up 2>/dev/null && log_info "hci0 (UD100) UP" || log_warn "Could not bring up hci0"
    hciconfig hci1 up 2>/dev/null && log_info "hci1 (AX210) UP" || log_warn "Could not bring up hci1"

    # Configure adapters for optimal scanning
    for iface in hci0 hci1; do
        if hciconfig $iface 2>/dev/null | grep -q "UP RUNNING"; then
            # Enable inquiry scan mode
            hciconfig $iface iscan 2>/dev/null || true
            hciconfig $iface pscan 2>/dev/null || true
        fi
    done

    # List available adapters
    echo ""
    log_info "Available Bluetooth adapters:"
    hciconfig -a 2>/dev/null | grep -E "^hci|BD Address|UP RUNNING" || log_warn "No adapters found"
    echo ""

    # Count active adapters
    ACTIVE_COUNT=$(hciconfig 2>/dev/null | grep -c "UP RUNNING" || echo "0")
    if [ "$ACTIVE_COUNT" -ge 2 ]; then
        log_info "Dual-adapter mode enabled: $ACTIVE_COUNT adapters active"
        echo -e "${GREEN}  Parallel scanning with dual adapters enabled!${NC}"
    else
        log_info "Single-adapter mode: $ACTIVE_COUNT adapter(s) active"
    fi
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

# Restart flag file
RESTART_FLAG="/tmp/bluek9_restart"

# Start the application with auto-restart support
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

    # Remove any stale restart flag
    rm -f "$RESTART_FLAG"

    # Run in a loop to support restart
    while true; do
        log_info "Starting BlueK9 application..."
        python app.py
        EXIT_CODE=$?

        # Check if restart was requested
        if [ -f "$RESTART_FLAG" ]; then
            rm -f "$RESTART_FLAG"
            echo ""
            log_info "Restart requested - restarting BlueK9..."

            # Pull latest updates if available
            if [ "$SKIP_UPDATE" = false ]; then
                auto_update
            fi

            sleep 2
            continue
        fi

        # If exit was not restart, break the loop
        if [ $EXIT_CODE -ne 0 ]; then
            log_error "BlueK9 exited with code $EXIT_CODE"
        fi
        break
    done
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
    echo "  --install     Install systemd service for auto-start"
    echo "  --uninstall   Remove systemd service"
    echo "  --help        Show this help message"
    echo ""
    echo "Systemd Service:"
    echo "  To start BlueK9 automatically on boot:"
    echo "    sudo $0 --install"
    echo ""
    echo "  To remove auto-start:"
    echo "    sudo $0 --uninstall"
    echo ""
}

# Install systemd service
install_service() {
    log_info "Installing BlueK9 systemd service..."

    SERVICE_FILE="$SCRIPT_DIR/bluek9.service"
    if [ ! -f "$SERVICE_FILE" ]; then
        log_error "Service file not found: $SERVICE_FILE"
        exit 1
    fi

    # Copy service file
    cp "$SERVICE_FILE" /etc/systemd/system/bluek9.service

    # Reload systemd
    systemctl daemon-reload

    # Enable service
    systemctl enable bluek9.service

    log_info "BlueK9 service installed and enabled"
    echo ""
    echo "Commands:"
    echo "  Start now:     sudo systemctl start bluek9"
    echo "  Stop:          sudo systemctl stop bluek9"
    echo "  View logs:     sudo journalctl -u bluek9 -f"
    echo "  Status:        sudo systemctl status bluek9"
    echo ""
}

# Uninstall systemd service
uninstall_service() {
    log_info "Removing BlueK9 systemd service..."

    # Stop if running
    systemctl stop bluek9.service 2>/dev/null || true

    # Disable service
    systemctl disable bluek9.service 2>/dev/null || true

    # Remove service file
    rm -f /etc/systemd/system/bluek9.service

    # Reload systemd
    systemctl daemon-reload

    log_info "BlueK9 service removed"
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
            --install)
                check_root
                install_service
                exit 0
                ;;
            --uninstall)
                check_root
                uninstall_service
                exit 0
                ;;
            --help|-h)
                show_help
                exit 0
                ;;
        esac
    done

    check_root

    # Auto-update from GitHub (unless skipped)
    if [ "$SKIP_UPDATE" = false ]; then
        auto_update
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
