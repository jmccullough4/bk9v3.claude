#!/bin/bash
#
# BlueK9 WARHAMMER Plugin Installation Script
# Installs BlueK9 as a plugin in an existing WARHAMMER installation
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo -e "${CYAN}╔════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   BlueK9 WARHAMMER Plugin Installer        ║${NC}"
echo -e "${CYAN}╚════════════════════════════════════════════╝${NC}"
echo ""

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}This script must be run as root${NC}"
    exit 1
fi

# Check for Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}Docker is required but not installed${NC}"
    exit 1
fi

# Default WARHAMMER directory
WARHAMMER_DIR="${WARHAMMER_DIR:-/apps/WARHAMMER}"

# Check if WARHAMMER exists
if [ ! -d "$WARHAMMER_DIR" ]; then
    echo -e "${YELLOW}WARHAMMER installation not found at $WARHAMMER_DIR${NC}"
    read -p "Enter WARHAMMER installation path: " WARHAMMER_DIR
    if [ ! -d "$WARHAMMER_DIR" ]; then
        echo -e "${RED}Directory does not exist: $WARHAMMER_DIR${NC}"
        exit 1
    fi
fi

echo -e "${GREEN}WARHAMMER found at: $WARHAMMER_DIR${NC}"
echo ""

# Build BlueK9 Docker image
echo -e "${CYAN}Building BlueK9 Docker image...${NC}"
cd "$PROJECT_DIR"
docker build -t bluek9:latest -f Dockerfile .

if [ $? -ne 0 ]; then
    echo -e "${RED}Failed to build BlueK9 Docker image${NC}"
    exit 1
fi

echo -e "${GREEN}BlueK9 image built successfully${NC}"
echo ""

# Check if plugins.json exists in WARHAMMER
PLUGINS_FILE="$WARHAMMER_DIR/plugins.json"

if [ -f "$PLUGINS_FILE" ]; then
    echo -e "${CYAN}Adding BlueK9 to WARHAMMER plugins...${NC}"

    # Check if BlueK9 is already registered
    if grep -q "wh-plugin-bluek9" "$PLUGINS_FILE" 2>/dev/null; then
        echo -e "${YELLOW}BlueK9 is already registered in WARHAMMER${NC}"
    else
        # Add BlueK9 to plugins.json
        # This is a simple approach - in production, use jq for proper JSON handling
        python3 << EOF
import json
import sys

try:
    with open('$PLUGINS_FILE', 'r') as f:
        config = json.load(f)
except:
    config = {'plugins': []}

# BlueK9 plugin configuration
bluek9_plugin = {
    "id": "wh-plugin-bluek9",
    "name": "BlueK9",
    "image": "bluek9:latest",
    "ports": [],
    "env": {
        "FLASK_ENV": "production",
        "PYTHONUNBUFFERED": "1",
        "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/run/dbus/system_bus_socket",
        "BLUEK9_DOCKER_MODE": "true",
        "BLUEK9_INSTALL_DIR": "/app"
    },
    "volumes": [
        "/var/run/dbus:/var/run/dbus",
        "/run/dbus/system_bus_socket:/run/dbus/system_bus_socket",
        "/dev:/dev",
        "/var/lib/bluetooth:/var/lib/bluetooth",
        "/etc/bluetooth:/etc/bluetooth:ro"
    ],
    "network": "host",
    "restart_policy": "unless-stopped"
}

# Add if not exists
if not any(p.get('id') == 'wh-plugin-bluek9' for p in config.get('plugins', [])):
    config['plugins'].append(bluek9_plugin)
    with open('$PLUGINS_FILE', 'w') as f:
        json.dump(config, f, indent=2)
    print("BlueK9 added to WARHAMMER plugins")
else:
    print("BlueK9 already registered")
EOF
    fi
else
    echo -e "${CYAN}Creating WARHAMMER plugins.json...${NC}"
    cat > "$PLUGINS_FILE" << 'PEOF'
{
  "plugins": [
    {
      "id": "wh-plugin-bluek9",
      "name": "BlueK9",
      "image": "bluek9:latest",
      "ports": [],
      "env": {
        "FLASK_ENV": "production",
        "PYTHONUNBUFFERED": "1",
        "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/run/dbus/system_bus_socket",
        "BLUEK9_DOCKER_MODE": "true",
        "BLUEK9_INSTALL_DIR": "/app"
      },
      "volumes": [
        "/var/run/dbus:/var/run/dbus",
        "/run/dbus/system_bus_socket:/run/dbus/system_bus_socket",
        "/dev:/dev",
        "/var/lib/bluetooth:/var/lib/bluetooth",
        "/etc/bluetooth:/etc/bluetooth:ro"
      ],
      "network": "host",
      "restart_policy": "unless-stopped"
    }
  ]
}
PEOF
    echo -e "${GREEN}Created plugins.json with BlueK9${NC}"
fi

echo ""
echo -e "${GREEN}╔════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   BlueK9 Plugin Installation Complete!     ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════╝${NC}"
echo ""
echo -e "BlueK9 is now available in WARHAMMER's Plugins section."
echo -e "Start BlueK9 from the WARHAMMER UI or run:"
echo -e "  ${CYAN}docker run -d --name wh-plugin-bluek9 --network host --privileged bluek9:latest${NC}"
echo ""
echo -e "Access BlueK9 at: ${CYAN}http://localhost:5000${NC}"
echo -e "Default login: ${YELLOW}bluek9 / warhammer${NC}"
echo ""
