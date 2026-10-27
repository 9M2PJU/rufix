#!/bin/sh
# Rufix one-liner installer.
#
#   curl -fsSL https://raw.githubusercontent.com/thuutien/rufix/main/packaging/quick-install.sh | sudo bash
#
# Pick a version, skip dependency installation, or uninstall:
#
#   curl -fsSL .../quick-install.sh | sudo bash -s -- 0.3.1
#   curl -fsSL .../quick-install.sh | sudo bash -s -- --no-deps
#   curl -fsSL .../quick-install.sh | sudo bash -s -- --uninstall
#
# Environment overrides: RUFIX_REPO (default thuutien/rufix),
# RUFIX_VERSION (default latest).
set -eu

REPO="${RUFIX_REPO:-thuutien/rufix}"
VERSION="${RUFIX_VERSION:-latest}"
INSTALL_DEPS=1
UNINSTALL=0

for arg in "$@"; do
    case "$arg" in
        --no-deps) INSTALL_DEPS=0 ;;
        --deps) INSTALL_DEPS=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help)
            echo "Usage: quick-install.sh [version] [--no-deps] [--uninstall]"
            exit 0 ;;
        *) VERSION="$arg" ;;
    esac
done

if [ "$(id -u)" != 0 ]; then
    echo "Please run as root (pipe into 'sudo bash')." >&2
    exit 1
fi
command -v curl >/dev/null || { echo "curl is required." >&2; exit 1; }

if [ "$VERSION" = "latest" ]; then
    TAG=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1)
    VERSION="${TAG#v}"
    [ -n "$VERSION" ] || { echo "Could not determine the latest release." >&2; exit 1; }
fi

install_deps() {
    if command -v apt-get >/dev/null; then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-gi gir1.2-gtk-3.0 \
            dosfstools ntfs-3g e2fsprogs util-linux policykit-1 udev parted
        # Mint 20 / older Ubuntu ship exfat-utils instead of exfatprogs.
        DEBIAN_FRONTEND=noninteractive apt-get install -y exfatprogs || \
            DEBIAN_FRONTEND=noninteractive apt-get install -y exfat-utils
    elif command -v dnf >/dev/null; then
        dnf install -y python3 python3-gobject gtk3 polkit \
            dosfstools ntfs-3g ntfsprogs exfatprogs e2fsprogs util-linux systemd parted
    elif command -v pacman >/dev/null; then
        pacman -Sy --needed --noconfirm python python-gobject gtk3 polkit \
            dosfstools ntfs-3g ntfsprogs exfatprogs e2fsprogs util-linux systemd parted
    elif command -v zypper >/dev/null; then
        zypper install -y python3 python3-gobject gtk3 polkit \
            dosfstools ntfs-3g ntfsprogs exfatprogs e2fsprogs util-linux systemd parted
    else
        echo "No supported package manager found; install dependencies manually" >&2
        echo "(Python 3 GObject bindings, GTK 3, dosfstools, ntfs-3g/ntfsprogs, exfatprogs," >&2
        echo " e2fsprogs, polkit, util-linux)." >&2
    fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM
cd "$TMP"
echo "Downloading Rufix $VERSION..."
curl -fsSL -o rufix.tar.gz "https://github.com/$REPO/releases/download/v$VERSION/rufix-$VERSION.tar.gz"
tar xzf rufix.tar.gz
cd "rufix-$VERSION"

if [ "$UNINSTALL" = 1 ]; then
    sh install.sh --uninstall
    exit 0
fi
[ "$INSTALL_DEPS" = 1 ] && install_deps
sh install.sh
echo "Rufix $VERSION installed. Run it with: rufix"
echo "Tip: for Windows ISO support install wimlib (wimtools on Debian/Ubuntu)."
