#!/bin/sh
# Build a distro-independent install archive: dist/rufix-<version>.tar.gz
# Users extract it and run: sudo ./install.sh
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/rufix/__init__.py")
mkdir -p "$SRC/dist"
git -C "$SRC" archive --format=tar.gz --prefix="rufix-$VERSION/" \
    -o "$SRC/dist/rufix-$VERSION.tar.gz" HEAD
