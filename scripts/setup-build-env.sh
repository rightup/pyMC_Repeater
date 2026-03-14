#!/bin/bash
# Setup Debian/Ubuntu build environment for pyMC_Repeater
# This script installs all required build dependencies using apt

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root or with sudo
if [ "$EUID" -ne 0 ]; then
    log_error "This script must be run with sudo or as root"
    exit 1
fi

log_info "Setting up build environment for pyMC_Repeater..."

# Update package list
log_info "Updating package lists..."
apt-get update

# Install Debian packaging tools
log_info "Installing Debian packaging tools..."
apt-get install -y \
    debhelper \
    devscripts \
    build-essential \
    fakeroot \
    lintian \
    git

# Install Python build dependencies
log_info "Installing Python build dependencies..."
apt-get install -y \
    dh-python \
    python3-all \
    python3-setuptools \
    python3-setuptools-scm \
    python3-wheel \
    python3-pip \
    python3-dev

# Install Python runtime dependencies (for building)
log_info "Installing Python runtime dependencies..."
apt-get install -y \
    python3-yaml \
    python3-cherrypy3 \
    python3-paho-mqtt \
    python3-psutil \
    python3-jwt || {
        log_warn "python3-jwt not available via apt, will be installed via pip during build"
    }

# Note: cherrypy-cors is not available in Debian repos
# For development testing: pip install cherrypy-cors

# Clean up
log_info "Cleaning up..."
apt-get autoremove -y
apt-get clean

log_info "Build environment setup complete!"
log_info ""
log_info "Next steps:"
log_info "  - Run './scripts/build-dev.sh' to build a development .deb package"
log_info "  - Run './scripts/build-prod.sh' to build a production .deb package (requires git tag)"
