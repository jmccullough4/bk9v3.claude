#!/bin/bash
#
# BlueK9 Update Script
# Run this from the host to update the Docker container with latest code
#
# Usage: ./update.sh [--force]
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}╔════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║       BlueK9 Update System             ║${NC}"
echo -e "${CYAN}╚════════════════════════════════════════╝${NC}"
echo ""

# Check if we're in a git repo
if [ ! -d ".git" ]; then
    echo -e "${RED}Error: Not a git repository. Please run from BlueK9 directory.${NC}"
    exit 1
fi

# Get current version
CURRENT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")

echo -e "${YELLOW}Current version:${NC} $CURRENT_COMMIT (branch: $CURRENT_BRANCH)"

# Fetch latest from remote
echo -e "\n${CYAN}Checking for updates...${NC}"
git fetch origin 2>/dev/null

# Check if there are updates
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse @{u} 2>/dev/null || echo "$LOCAL")

if [ "$LOCAL" = "$REMOTE" ]; then
    echo -e "${GREEN}Already up to date!${NC}"
    if [ "$1" != "--force" ]; then
        echo -e "Use ${YELLOW}./update.sh --force${NC} to rebuild anyway."
        exit 0
    fi
    echo -e "${YELLOW}Force rebuild requested...${NC}"
else
    # Show what's new
    echo -e "\n${GREEN}Updates available:${NC}"
    git log --oneline HEAD..@{u} 2>/dev/null | head -10

    BEHIND=$(git rev-list --count HEAD..@{u} 2>/dev/null || echo "?")
    echo -e "\n${YELLOW}$BEHIND commit(s) behind remote${NC}"
fi

# Confirm update
if [ "$1" != "--force" ] && [ "$1" != "-y" ]; then
    echo ""
    read -p "Proceed with update? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${RED}Update cancelled.${NC}"
        exit 1
    fi
fi

# Stop running container
echo -e "\n${CYAN}Stopping BlueK9 container...${NC}"
docker-compose down 2>/dev/null || docker stop bluek9 2>/dev/null || true

# Pull latest code
echo -e "\n${CYAN}Pulling latest code...${NC}"
git pull origin "$CURRENT_BRANCH"

NEW_COMMIT=$(git rev-parse --short HEAD)
echo -e "${GREEN}Updated to:${NC} $NEW_COMMIT"

# Rebuild container
echo -e "\n${CYAN}Rebuilding Docker container...${NC}"
docker-compose build --no-cache

# Start container
echo -e "\n${CYAN}Starting BlueK9...${NC}"
docker-compose up -d

# Wait for startup
echo -e "\n${CYAN}Waiting for startup...${NC}"
sleep 3

# Check if running
if docker ps | grep -q bluek9; then
    echo -e "\n${GREEN}╔════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║       Update Complete!                 ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════╝${NC}"
    echo -e "\nVersion: ${YELLOW}$NEW_COMMIT${NC}"
    echo -e "Access at: ${CYAN}http://localhost:5000${NC}"
else
    echo -e "\n${RED}Warning: Container may not have started correctly.${NC}"
    echo -e "Check logs with: ${YELLOW}docker-compose logs${NC}"
fi
