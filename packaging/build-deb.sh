#!/bin/sh
# Build a .deb package for Linux Mint / Ubuntu / Debian.
# Usage: ./packaging/build-deb.sh   ->   dist/rufix_<version>_all.deb
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/rufix/__init__.py")
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

chmod 755 "$STAGE"
sh "$SRC/install.sh" --prefix-root "$STAGE"
install -d "$STAGE/DEBIAN"
cat > "$STAGE/DEBIAN/control" <<CTRL
Package: rufix
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.6), python3-gi, gir1.2-gtk-3.0, util-linux, fdisk | util-linux (<< 2.29.2-3~), dosfstools, e2fsprogs, ntfs-3g, exfatprogs | exfat-utils, pkexec | policykit-1, udev
Recommends: wimtools, 7zip | p7zip-full
Suggests: unrar
Maintainer: Rufix <rufix@localhost>
Description: Create bootable USB drives and format USB keys
 A Rufus-style GTK utility for Linux. Formats USB flash drives (FAT32,
 exFAT, NTFS, ext4), writes Linux ISO/IMG images in DD mode (including
 .xz/.gz/.bz2 compressed images) and creates UEFI-bootable Windows
 installation drives from Windows ISOs.
CTRL
cat > "$STAGE/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
POST
chmod 755 "$STAGE/DEBIAN/postinst"
mkdir -p "$SRC/dist"
dpkg-deb --root-owner-group --build "$STAGE" "$SRC/dist/rufix_${VERSION}_all.deb"
