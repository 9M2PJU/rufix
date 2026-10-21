"""Inspect disk images (ISO / raw .img, optionally compressed) without root.

This reads the ISO9660 volume descriptors and root directory directly, so no
mounting or external tools are required.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import List, Optional

SECTOR = 2048
COMPRESSED_EXT = (".gz", ".xz", ".bz2")
RAW_EXT = (".img", ".raw", ".bin", ".dd")


@dataclass
class ImageInfo:
    path: str
    size: int
    kind: str                     # "iso", "img", or "unknown"
    compression: Optional[str] = None
    label: str = ""
    is_hybrid: bool = False       # has an MBR/GPT so it can be written raw
    has_udf: bool = False
    has_efi: bool = False         # El Torito UEFI boot entry
    has_bios: bool = False        # El Torito x86 BIOS boot entry
    is_windows: bool = False
    root_entries: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def filename(self) -> str:
        return os.path.basename(self.path)

    @property
    def supports_iso_mode(self) -> bool:
        return self.kind == "iso" and self.compression is None

    @property
    def supports_dd_mode(self) -> bool:
        # Windows ISOs have no partition table; raw-writing them won't boot.
        return self.kind == "img" or (self.kind == "iso" and not self.is_windows)

    @property
    def recommended_mode(self) -> str:
        if not self.supports_iso_mode:
            return "dd"
        if self.is_windows:
            return "iso"
        return "dd" if self.is_hybrid else "iso"


def _read(f, offset: int, length: int) -> bytes:
    f.seek(offset)
    return f.read(length)


def _has_partition_table(head: bytes) -> bool:
    if len(head) < 512 or head[510:512] != b"\x55\xaa":
        return False
    # At least one non-empty MBR partition entry.
    for i in range(4):
        entry = head[446 + i * 16: 446 + (i + 1) * 16]
        if entry[4] != 0 and struct.unpack_from("<I", entry, 12)[0] > 0:
            return True
    return False


def _iso_name(raw: bytes) -> str:
    name = raw.decode("ascii", "replace")
    return name.split(";")[0].rstrip(".").upper()


def _list_dir(f, extent: int, size: int) -> List[tuple]:
    """Return (name, is_dir, extent, size) records in an ISO9660 directory."""
    entries = []
    data = _read(f, extent * SECTOR, min(size, 1 << 20))
    pos = 0
    while pos < len(data):
        rec_len = data[pos]
        if rec_len == 0:
            # Records never cross sector boundaries: skip to next sector.
            pos = (pos // SECTOR + 1) * SECTOR
            continue
        rec = data[pos:pos + rec_len]
        if len(rec) < 34:
            break
        name_len = rec[32]
        name_raw = rec[33:33 + name_len]
        flags = rec[25]
        ext = struct.unpack_from("<I", rec, 2)[0]
        sz = struct.unpack_from("<I", rec, 10)[0]
        if name_raw not in (b"\x00", b"\x01"):
            entries.append((_iso_name(name_raw), bool(flags & 2), ext, sz))
        pos += rec_len
    return entries


def _parse_el_torito(f, catalog_sector: int, info: ImageInfo) -> None:
    cat = _read(f, catalog_sector * SECTOR, SECTOR)
    if len(cat) < 64 or cat[0] != 0x01:
        return
    platforms = {cat[1]}
    pos = 64
    while pos + 32 <= len(cat):
        hdr = cat[pos]
        if hdr in (0x90, 0x91):          # section header
            platforms.add(cat[pos + 1])
        elif hdr == 0x00 and pos > 64 and cat[pos:pos + 32] == bytes(32):
            break
        pos += 32
    info.has_bios = 0x00 in platforms
    info.has_efi = 0xEF in platforms


def _inspect_iso(f, info: ImageInfo) -> None:
    root = None
    for i in range(16, 64):
        vd = _read(f, i * SECTOR, SECTOR)
        if len(vd) < 7:
            break
        ident = vd[1:6]
        if ident in (b"BEA01", b"NSR02", b"NSR03", b"TEA01"):
            if ident.startswith(b"NSR"):
                info.has_udf = True
            continue
        if ident != b"CD001":
            continue
        vtype = vd[0]
        if vtype == 1:                       # primary volume descriptor
            info.kind = "iso"
            info.label = vd[40:72].decode("ascii", "replace").strip()
            root_rec = vd[156:190]
            root = (struct.unpack_from("<I", root_rec, 2)[0],
                    struct.unpack_from("<I", root_rec, 10)[0])
        elif vtype == 0 and vd[7:30].rstrip(b"\x00").startswith(b"EL TORITO"):
            _parse_el_torito(f, struct.unpack_from("<I", vd, 71)[0], info)
        elif vtype == 255:
            # UDF descriptors follow the ISO9660 terminator; keep scanning a bit.
            continue

    if root is None:
        return
    entries = _list_dir(f, *root)
    info.root_entries = sorted(e[0] for e in entries)
    names = {e[0]: e for e in entries}

    if "BOOTMGR" in names:
        info.is_windows = True
    sources = names.get("SOURCES")
    if sources and sources[1]:
        sub = {e[0] for e in _list_dir(f, sources[2], sources[3])}
        if sub & {"INSTALL.WIM", "INSTALL.ESD", "BOOT.WIM"}:
            info.is_windows = True
    # Modern Windows ISOs expose only README.TXT through ISO9660; the real
    # tree lives in UDF.
    if info.has_udf and set(names) <= {"README.TXT"}:
        info.is_windows = True
    if "EFI" in names:
        info.has_efi = True


def inspect(path: str) -> ImageInfo:
    size = os.path.getsize(path)
    lower = path.lower()
    info = ImageInfo(path=path, size=size, kind="unknown")

    for ext in COMPRESSED_EXT:
        if lower.endswith(ext):
            info.compression = ext[1:]
            info.kind = "img"
            info.notes.append(f"Compressed image ({info.compression}); it will be "
                              "decompressed on the fly and written in DD mode.")
            return info

    with open(path, "rb") as f:
        head = _read(f, 0, 512)
        info.is_hybrid = _has_partition_table(head)
        _inspect_iso(f, info)

    if info.kind == "iso":
        if info.is_windows:
            info.notes.append("Windows installation media detected.")
        elif info.is_hybrid:
            info.notes.append("ISOHybrid image detected (can be written in DD mode).")
    elif info.is_hybrid or lower.endswith(RAW_EXT):
        info.kind = "img"
        info.notes.append("Raw disk image; it will be written in DD mode.")
    return info
