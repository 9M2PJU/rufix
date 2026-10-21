"""Enumerate removable / USB block devices using lsblk."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

# Mount points that mark a disk as a system disk we must never touch.
SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "[SWAP]"}

_COLUMNS_NEW = "NAME,PATH,SIZE,TYPE,TRAN,RM,HOTPLUG,MODEL,VENDOR,LABEL,MOUNTPOINTS,FSTYPE"
_COLUMNS_OLD = "NAME,PATH,SIZE,TYPE,TRAN,RM,HOTPLUG,MODEL,VENDOR,LABEL,MOUNTPOINT,FSTYPE"


def _flag(value) -> bool:
    """lsblk emits booleans as true/false (new) or "0"/"1" (old)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip() in ("1", "true")


def _mounts(node: dict) -> List[str]:
    if "mountpoints" in node:
        return [m for m in (node.get("mountpoints") or []) if m]
    mp = node.get("mountpoint")
    return [mp] if mp else []


def human_size(n: int) -> str:
    """Rufus-style size string (decimal units, like drive vendors use)."""
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1000 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1000
    return f"{n} B"


@dataclass
class Partition:
    path: str
    size: int
    fstype: str
    label: str
    mountpoints: List[str]


@dataclass
class Device:
    path: str
    name: str
    size: int
    tran: str
    removable: bool
    hotplug: bool
    model: str
    vendor: str
    partitions: List[Partition] = field(default_factory=list)
    mountpoints: List[str] = field(default_factory=list)

    @property
    def is_usb(self) -> bool:
        return self.tran == "usb"

    @property
    def is_removable_media(self) -> bool:
        """USB flash drives and SD cards report RM=1."""
        return self.removable

    @property
    def all_mounts(self) -> List[str]:
        mounts = list(self.mountpoints)
        for p in self.partitions:
            mounts.extend(p.mountpoints)
        return mounts

    @property
    def is_system(self) -> bool:
        return any(m in SYSTEM_MOUNTS for m in self.all_mounts)

    @property
    def display_label(self) -> str:
        for p in self.partitions:
            if p.label:
                return p.label
        return ""

    @property
    def display_name(self) -> str:
        label = self.display_label
        desc = " ".join(x for x in (self.vendor, self.model) if x).strip() or self.name
        head = f"{label} ({self.path}) " if label else f"{self.path} "
        return f"{head}[{human_size(self.size)}] - {desc}"


def _run_lsblk() -> dict:
    for cols in (_COLUMNS_NEW, _COLUMNS_OLD):
        try:
            out = subprocess.run(
                ["lsblk", "-J", "-b", "-o", cols],
                check=True, capture_output=True, text=True,
            ).stdout
            return json.loads(out)
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            continue
    return {"blockdevices": []}


def _strip(v) -> str:
    return (v or "").strip()


def _mmc_type(name: str) -> str:
    try:
        with open(f"/sys/block/{name}/device/type") as f:
            return f.read().strip()
    except OSError:
        return ""


def list_devices(include_usb_hdd: bool = False) -> List[Device]:
    """Return candidate target drives.

    By default only removable media (flash drives, SD cards) are listed, like
    Rufus. With include_usb_hdd, fixed USB disks are listed too. Internal
    (SATA/NVMe) disks and any disk hosting the running system are never listed.
    """
    data = _run_lsblk()
    devices: List[Device] = []
    for node in data.get("blockdevices", []):
        if node.get("type") != "disk":
            continue
        size = int(node.get("size") or 0)
        if size <= 0:
            continue
        path = node.get("path") or f"/dev/{node['name']}"
        dev = Device(
            path=path,
            name=node["name"],
            size=size,
            tran=_strip(node.get("tran")),
            removable=_flag(node.get("rm")),
            hotplug=_flag(node.get("hotplug")),
            model=_strip(node.get("model")),
            vendor=_strip(node.get("vendor")),
            mountpoints=_mounts(node),
        )
        for child in node.get("children", []) or []:
            dev.partitions.append(Partition(
                path=child.get("path") or f"/dev/{child['name']}",
                size=int(child.get("size") or 0),
                fstype=_strip(child.get("fstype")),
                label=_strip(child.get("label")),
                mountpoints=_mounts(child),
            ))
            # Nested children (e.g. LUKS/LVM) - collect mounts so the system
            # disk check still works.
            for grand in child.get("children", []) or []:
                dev.partitions[-1].mountpoints.extend(_mounts(grand))

        if dev.is_system:
            continue
        if dev.name.startswith(("loop", "ram", "zram", "sr", "dm-", "md")):
            continue
        if dev.name.startswith("mmcblk"):
            # SD cards are fine; soldered eMMC (often the system disk) is not.
            if _mmc_type(dev.name) == "SD":
                devices.append(dev)
        elif dev.is_removable_media and (dev.is_usb or dev.hotplug):
            devices.append(dev)
        elif include_usb_hdd and dev.is_usb:
            devices.append(dev)
    return devices
