#!/bin/bash
#
# BlueK9 Update Script
# Simple script to pull latest changes from git
#

INSTALL_DIR="/apps/bk9v3.claude"

echo "========================================"
echo "BlueK9 Update Script"
echo "========================================"

# Change to install directory
cd "$INSTALL_DIR" || {
    echo "ERROR: Could not change to $INSTALL_DIR"
    exit 1
}

echo "Current directory: $(pwd)"
echo "Current commit: $(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
echo ""

# Pull latest changes
echo "Pulling latest changes..."
git pull

if [ $? -eq 0 ]; then
    echo ""
    echo "Update successful!"
    echo "New commit: $(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
    echo ""
    echo "NOTE: Restart BlueK9 to apply changes:"
    echo "  sudo systemctl restart bluek9"
    echo "  OR use the Restart button in the UI"
else
    echo ""
    echo "ERROR: git pull failed"
    exit 1
fi
