# Rufix

A Rufus-style USB formatting and bootable-drive tool for Linux, built with
Python + GTK3. Made for **Linux Mint** (20, 21, 22 / LMDE) and works on
Ubuntu, Debian and other GTK desktops.

## Features

- Lists only USB flash drives and SD cards (fixed USB disks behind the
  *List USB Hard Drives* option). Internal disks and the disk running your
  system are never shown, and the root helper re-checks this.
- **Non bootable format**: FAT32, exFAT, NTFS, ext4, FAT16 · MBR or GPT ·
  volume label · cluster size · quick or full (zero-fill) format.
- **Bad blocks check** (1–4 write/read passes, like Rufus).
- **Linux ISOs / disk images** written in **DD mode** with read-back
  verification. Accepts `.iso`, `.img`, `.raw` and compressed
  `.img.xz` / `.img.gz` / `.img.bz2` (decompressed on the fly).
- **ISO mode (file copy)**: FAT32 UEFI-bootable drive from an ISO.
- **Windows 10/11 ISOs**: UEFI-bootable install drive.
  - `install.wim` > 4 GB is split automatically if `wimtools` is installed;
    otherwise a FAT32 BOOT + NTFS INSTALL two-partition layout is used.
  - Rufus-style *Windows User Experience* options: skip TPM 2.0 / Secure
    Boot / 4GB RAM checks, skip the online Microsoft account, skip privacy
    questions (via `autounattend.xml`).
- MD5 / SHA-1 / SHA-256 checksum of the image (`#` button).
- Drag & drop an image onto the window; open images from the file manager.
- Progress, speed and time remaining, cancel, and a saveable log.

## Install on Linux Mint / Ubuntu / Debian

Download `rufix_0.1_all.deb` from the
[latest release](https://github.com/thuutien/rufix/releases/latest) and run:

```bash
sudo apt install ./rufix_0.1_all.deb
```

`apt` pulls in all dependencies. For splitting large Windows images, also
install `wimtools`.

## Install on other distributions

Download `rufix-0.1.tar.gz` from the
[latest release](https://github.com/thuutien/rufix/releases/latest), install
the dependencies (Python 3 GObject bindings, GTK 3, dosfstools, ntfs-3g,
exfatprogs, e2fsprogs, polkit), then:

```bash
tar xzf rufix-0.1.tar.gz && cd rufix-0.1 && sudo ./install.sh
```

## Build the packages yourself

```bash
./packaging/build-deb.sh       # dist/rufix_<version>_all.deb
./packaging/build-tarball.sh   # dist/rufix-<version>.tar.gz
```

Rufix then appears in the menu under **Administration / System Tools**.
To remove it: `sudo ./install.sh --uninstall` (or `sudo apt remove rufix`).

On Mint 20 replace `exfatprogs` with `exfat-utils` (both are supported).

## Run from source

```bash
./rufix.py
```

## How it works

The GUI runs as your normal user. When you press START, it launches
`rufix-helper` through `pkexec` (you get the standard password prompt),
sends it the job as JSON, and the helper streams progress back. Only the
helper runs as root, and it refuses any device that is not a removable/USB
whole disk or that holds a system mount point.

## Limitations

- Bootable drives made in **ISO mode** are UEFI-only (no legacy BIOS boot
  loader is installed). For Linux ISOs, DD mode is the default and boots on
  both BIOS and UEFI.
- Windows-to-Go, persistent partitions and FreeDOS are not implemented.
