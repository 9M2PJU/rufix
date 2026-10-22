#!/bin/sh
# Build an RPM for Fedora / RHEL / openSUSE and derivatives.
# Usage: ./packaging/build-rpm.sh   ->   dist/rufix-<version>-1.noarch.rpm
set -e
command -v rpmbuild >/dev/null || {
    echo "rpmbuild not found. Install it with:" >&2
    echo "  Fedora/RHEL: sudo dnf install rpm-build" >&2
    echo "  openSUSE:    sudo zypper install rpm-build" >&2
    exit 1
}
SRC="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/rufix/__init__.py")
TOPDIR="$(mktemp -d)"
trap 'rm -rf "$TOPDIR"' EXIT INT TERM
mkdir -p "$TOPDIR"/BUILD "$TOPDIR"/RPMS "$TOPDIR"/SOURCES "$TOPDIR"/SPECS "$TOPDIR"/SRPMS "$SRC/dist"
git -C "$SRC" archive --format=tar.gz --prefix="rufix-$VERSION/" \
    -o "$TOPDIR/SOURCES/rufix-$VERSION.tar.gz" HEAD
rpmbuild -bb --define "ver $VERSION" --define "_topdir $TOPDIR" "$SRC/packaging/rufix.spec"
cp "$TOPDIR"/RPMS/noarch/*.rpm "$SRC/dist/"
ls "$SRC/dist"/*.rpm
