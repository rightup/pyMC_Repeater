#!/bin/bash
# Build production .deb package for pyMC_Repeater
# Requires a clean git tag - fails if not on a tagged commit

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
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

log_step() {
    echo -e "${BLUE}[STEP]${NC} $1"
}

# Change to project root
cd "$(dirname "$0")/.."
PROJECT_ROOT=$(pwd)

log_info "Building production .deb package for pyMC_Repeater..."
log_info "Project root: $PROJECT_ROOT"

# Check if we're in a git repository
if [ ! -d .git ]; then
    log_error "Not in a git repository. Cannot use setuptools_scm."
    exit 1
fi

# Check for uncommitted changes
if ! git diff-index --quiet HEAD --; then
    log_error "You have uncommitted changes. Production builds require a clean git state."
    log_error "Please commit or stash your changes first."
    exit 1
fi

# Check if current commit is tagged
log_step "Verifying git tag..."
CURRENT_COMMIT=$(git rev-parse HEAD)
TAG=$(git describe --exact-match --tags "$CURRENT_COMMIT" 2>/dev/null || echo "")

if [ -z "$TAG" ]; then
    log_error "Current commit is not tagged."
    log_error "Production builds require a git tag."
    log_error ""
    log_error "To create a tag:"
    log_error "  git tag v1.0.5"
    log_error "  git push origin v1.0.5"
    log_error ""
    log_error "For development builds, use: ./scripts/build-dev.sh"
    exit 1
fi

log_info "Building from tag: $TAG"

# Get version from setuptools_scm
log_step "Detecting version from git tag..."
if ! command -v python3 &> /dev/null; then
    log_error "python3 not found. Please install python3."
    exit 1
fi

# Try to get version from setuptools_scm
VERSION=$(python3 -m setuptools_scm 2>/dev/null || echo "")

if [ -z "$VERSION" ]; then
    log_error "Failed to get version from setuptools_scm."
    log_error "Make sure setuptools_scm is installed: pip3 install setuptools_scm"
    exit 1
fi

log_info "Detected version: $VERSION"

# Verify version doesn't contain dev/post markers (should be clean release version)
if echo "$VERSION" | grep -qE '(dev|post|\+)'; then
    log_error "Version '$VERSION' contains development markers."
    log_error "Production builds must be from a clean tag without uncommitted changes."
    log_error "Current tag: $TAG"
    log_error ""
    log_error "This might happen if:"
    log_error "  - There are commits after the tag"
    log_error "  - The working directory is dirty"
    log_error "  - The tag format is not recognized"
    exit 1
fi

# Use the version as-is for Debian (it should be clean like "1.0.5")
DEBIAN_VERSION="$VERSION"

log_info "Debian version: $DEBIAN_VERSION"

# Update debian/changelog with production release info
log_step "Updating debian/changelog..."
CHANGELOG_DATE=$(date -R)
cat > debian/changelog << EOF
pymc-repeater ($DEBIAN_VERSION) stable; urgency=medium

  * Production release $VERSION
  * Git tag: $TAG
  * Commit: $(git rev-parse --short HEAD)

 -- Lloyd <lloyd@rightup.co.uk>  $CHANGELOG_DATE
EOF

log_info "Changelog updated with version $DEBIAN_VERSION"

# Clean previous builds
log_step "Cleaning previous builds..."
rm -rf debian/pymc-repeater/
rm -rf debian/.debhelper/
rm -rf debian/files
rm -f debian/pymc-repeater.*.debhelper
rm -f debian/pymc-repeater.substvars
rm -f debian/*.log
rm -rf .pybuild/
rm -rf build/
rm -rf *.egg-info/
rm -f repeater/_version.py
rm -f ../*.deb
rm -f ../*.buildinfo
rm -f ../*.changes

# Build the package
log_step "Building production .deb package..."
log_info "This may take a few minutes..."

# Build without signing (can be signed later if needed)
if debuild -us -uc -b 2>&1 | tee /tmp/debuild-prod.log; then
    log_info "Build successful!"
else
    log_error "Build failed. Check /tmp/debuild-prod.log for details."
    exit 1
fi

# Find and display the built package
DEB_FILE=$(find .. -maxdepth 1 -name "pymc-repeater_${DEBIAN_VERSION}_*.deb" -type f | head -n 1)

if [ -n "$DEB_FILE" ]; then
    # Run lintian to check package quality
    log_step "Running lintian checks..."
    lintian "$DEB_FILE" || log_warn "Lintian found some issues (non-fatal)"

    log_info ""
    log_info "════════════════════════════════════════════════════════════"
    log_info "Production build complete!"
    log_info "Package: $(basename "$DEB_FILE")"
    log_info "Location: $DEB_FILE"
    log_info "Size: $(du -h "$DEB_FILE" | cut -f1)"
    log_info "Version: $VERSION"
    log_info "Git tag: $TAG"
    log_info "════════════════════════════════════════════════════════════"
    log_info ""
    log_info "To install:"
    log_info "  sudo dpkg -i $DEB_FILE"
    log_info ""
    log_info "To inspect package contents:"
    log_info "  dpkg-deb -c $DEB_FILE"
    log_info ""
    log_info "To check package info:"
    log_info "  dpkg-deb -I $DEB_FILE"
    log_info ""
    log_info "To sign the package:"
    log_info "  debsign $DEB_FILE"
else
    log_warn "Built package not found in expected location"
    log_info "Searching for .deb files in parent directory..."
    ls -lh ../*.deb 2>/dev/null || log_warn "No .deb files found"
fi
