#!/usr/bin/env python3
"""Rufix privileged helper.

Rufix is a clone of Rufus (https://rufus.ie) for Linux: it formats USB drives
and creates bootable USB drives from ISO/IMG images. It is an independent
project and is not affiliated with Rufus or its authors.

Runs as root (via pkexec). Reads one JSON job description from the first line
of stdin, performs it, and reports progress as JSON lines on stdout:

    {"t": "log", "m": "..."}          - log line
    {"t": "status", "m": "..."}       - short status text for the progress bar
    {"t": "progress", "p": 0.42}      - fraction done (-1 = indeterminate)
    {"t": "done", "ok": true, "m": ""} - final result

Actions: "format", "dd" (raw image write), "iso" (ISO file copy) and
"extract" (format, then unpack a zip/7z/rar/tar archive onto the drive).

Any further line on stdin (or stdin closing) cancels the running job.

This file is intentionally self-contained (standard library only) so that it
can be executed directly by pkexec.
"""

import bz2
import gzip
import hashlib
import json
import lzma
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile

os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
os.environ["LC_ALL"] = "C"

MiB = 1024 * 1024
CHUNK = 4 * MiB
FAT32_MAX_FILE = 4 * 1024 * 1024 * 1024 - 1
SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "[SWAP]"}

GUID_BASIC_DATA = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
GUID_LINUX = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
MBR_TYPES = {"fat32": "c", "fat16": "e", "exfat": "7", "ntfs": "7", "ext4": "83"}

cancel_event = threading.Event()
_out_lock = threading.Lock()


class Cancelled(Exception):
    pass


class JobError(Exception):
    pass


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def emit(**msg):
    with _out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def log(m):
    emit(t="log", m=m)


def status(m):
    emit(t="status", m=m)


def progress(p):
    emit(t="progress", p=max(-1.0, min(1.0, p)))


def check_cancel():
    if cancel_event.is_set():
        raise Cancelled()


def _stdin_watcher():
    try:
        for _ in sys.stdin:
            break
    except Exception:
        pass
    cancel_event.set()


# --------------------------------------------------------------------------
# Command helpers
# --------------------------------------------------------------------------

def run(cmd, check=True, quiet=False):
    check_cancel()
    if not quiet:
        log("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          stdin=subprocess.DEVNULL, text=True, errors="replace")
    out = proc.stdout.strip()
    if out and not quiet:
        for line in out.splitlines()[-15:]:
            log("  " + line)
    if check and proc.returncode != 0:
        raise JobError(f"Command failed ({proc.returncode}): {' '.join(cmd)}")
    return proc


def have(tool):
    return shutil.which(tool) is not None


def settle():
    if have("udevadm"):
        run(["udevadm", "settle", "--timeout=10"], check=False, quiet=True)


# --------------------------------------------------------------------------
# Device validation and preparation
# --------------------------------------------------------------------------

def lsblk_device(dev):
    for col in ("MOUNTPOINTS", "MOUNTPOINT"):
        proc = subprocess.run(
            ["lsblk", "-J", "-b", "-o", f"NAME,PATH,TYPE,TRAN,RM,HOTPLUG,SIZE,{col}", dev],
            capture_output=True, text=True)
        if proc.returncode == 0:
            data = json.loads(proc.stdout)["blockdevices"]
            if data:
                return data[0]
    raise JobError(f"Cannot query {dev}")


def node_mounts(node):
    if "mountpoints" in node:
        mounts = [m for m in node.get("mountpoints") or [] if m]
    else:
        mounts = [node["mountpoint"]] if node.get("mountpoint") else []
    for child in node.get("children") or []:
        mounts += node_mounts(child)
    return mounts


def flag(v):
    return v is True or str(v).strip() in ("1", "true")


def validate_device(dev, allow_usb_hdd):
    if not re.fullmatch(r"/dev/(sd[a-z]+|mmcblk\d+|nvme\d+n\d+|vd[a-z]+)", dev):
        raise JobError(f"Refusing to use unexpected device path {dev}")
    if not stat.S_ISBLK(os.stat(dev).st_mode):
        raise JobError(f"{dev} is not a block device")
    node = lsblk_device(dev)
    if node.get("type") != "disk":
        raise JobError(f"{dev} is not a whole disk")
    mounts = node_mounts(node)
    if any(m in SYSTEM_MOUNTS for m in mounts):
        raise JobError(f"{dev} holds a system mount point - refusing")
    tran = (node.get("tran") or "").strip()
    removable = flag(node.get("rm"))
    name = os.path.basename(dev)
    if name.startswith("mmcblk"):
        try:
            with open(f"/sys/block/{name}/device/type") as f:
                if f.read().strip() != "SD":
                    raise JobError(f"{dev} is internal eMMC storage - refusing")
        except OSError:
            raise JobError(f"Cannot determine type of {dev}")
    elif removable and (tran == "usb" or flag(node.get("hotplug"))):
        pass
    elif tran == "usb":
        if not allow_usb_hdd:
            raise JobError(f"{dev} is a fixed USB disk; enable 'List USB Hard Drives' to use it")
    else:
        raise JobError(f"{dev} is not a USB/removable drive - refusing")
    return int(node.get("size") or 0)


def part_path(dev, n):
    return f"{dev}p{n}" if dev[-1].isdigit() else f"{dev}{n}"


def unmount_all(dev):
    node = lsblk_device(dev)
    mounts = sorted(set(node_mounts(node)), key=len, reverse=True)
    for m in mounts:
        if m == "[SWAP]":
            continue
        status("Unmounting...")
        if run(["umount", m], check=False).returncode != 0:
            run(["umount", "-l", m], check=False)
    settle()


def wipe_device(dev, size):
    """Remove all signatures plus the first/last MiB (old MBR/GPT copies)."""
    status("Clearing partition table...")
    for p in sorted(os.listdir("/dev")):
        full = "/dev/" + p
        if full.startswith(dev) and full != dev and re.fullmatch(r"p?\d+", full[len(dev):]):
            run(["wipefs", "-a", "-f", full], check=False, quiet=True)
    run(["wipefs", "-a", "-f", dev], check=False)
    fd = os.open(dev, os.O_WRONLY)
    try:
        zero = bytes(MiB)
        os.pwrite(fd, zero, 0)
        if size > 2 * MiB:
            os.pwrite(fd, zero, size - MiB)
        os.fsync(fd)
    finally:
        os.close(fd)
    reread_partitions(dev)


def reread_partitions(dev):
    settle()
    for _ in range(5):
        if have("partprobe") and run(["partprobe", dev], check=False, quiet=True).returncode == 0:
            break
        if run(["blockdev", "--rereadpt", dev], check=False, quiet=True).returncode == 0:
            break
        time.sleep(1)
    settle()


def wait_for(path, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(path):
            settle()
            return
        time.sleep(0.3)
    raise JobError(f"Partition {path} did not appear")


def create_partitions(dev, scheme, parts):
    """parts: list of (fs, size_spec or None, bootable)."""
    status("Creating partitions...")
    lines = ["label: " + ("gpt" if scheme == "gpt" else "dos"), "unit: sectors", ""]
    for i, (fs, size, bootable) in enumerate(parts):
        fields = []
        if i == 0:
            fields.append("start=2048")
        if size:
            fields.append(f"size={size}")
        if scheme == "gpt":
            fields.append("type=" + (GUID_LINUX if fs == "ext4" else GUID_BASIC_DATA))
        else:
            fields.append("type=" + MBR_TYPES[fs])
            if bootable:
                fields.append("bootable")
        lines.append(", ".join(fields))
    script = "\n".join(lines) + "\n"
    log("sfdisk script:\n" + script)
    proc = subprocess.run(["sfdisk", "--wipe", "always", "--wipe-partitions", "always", dev],
                          input=script, capture_output=True, text=True)
    for line in (proc.stdout + proc.stderr).strip().splitlines()[-10:]:
        log("  " + line)
    if proc.returncode != 0:
        raise JobError("Partitioning failed")
    reread_partitions(dev)
    paths = [part_path(dev, i + 1) for i in range(len(parts))]
    for p in paths:
        wait_for(p)
    return paths


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def sanitize_label(fs, label):
    label = label.strip()
    if fs in ("fat32", "fat16"):
        label = re.sub(r'[^A-Za-z0-9 _\-!#$%&\'()@^`{}~]', "_", label).upper()[:11]
    elif fs == "exfat":
        label = label[:11]
    elif fs == "ext4":
        label = label[:16]
    elif fs == "ntfs":
        label = label[:32]
    return label


def exfat_is_exfatprogs():
    proc = subprocess.run(["mkfs.exfat", "-V"], capture_output=True, text=True)
    return "exfatprogs" in (proc.stdout + proc.stderr)


def mkfs(fs, part, label, cluster):
    label = sanitize_label(fs, label)
    status(f"Formatting ({fs.upper()})...")
    progress(-1)
    if fs in ("fat32", "fat16"):
        cmd = ["mkfs.vfat", "-F", "32" if fs == "fat32" else "16"]
        if label:
            cmd += ["-n", label]
        if cluster:
            cmd += ["-s", str(max(1, cluster // 512))]
    elif fs == "exfat":
        cmd = ["mkfs.exfat"]
        if exfat_is_exfatprogs():
            if label:
                cmd += ["-L", label]
            if cluster:
                cmd += ["-c", str(cluster)]
        else:
            if label:
                cmd += ["-n", label]
            if cluster:
                cmd += ["-s", str(max(1, cluster // 512))]
    elif fs == "ntfs":
        cmd = ["mkntfs", "-Q", "-F"]
        if label:
            cmd += ["-L", label]
        if cluster:
            cmd += ["-c", str(cluster)]
    elif fs == "ext4":
        cmd = ["mkfs.ext4", "-F"]
        if label:
            cmd += ["-L", label]
        if cluster:
            cmd += ["-b", str(cluster)]
    else:
        raise JobError(f"Unsupported file system {fs}")
    tool = cmd[0]
    if not have(tool):
        pkg = {"mkfs.vfat": "dosfstools", "mkfs.exfat": "exfatprogs",
               "mkntfs": "ntfs-3g", "mkfs.ext4": "e2fsprogs"}[tool]
        raise JobError(f"{tool} not found - install it with: sudo apt install {pkg}")
    run(cmd + [part])


def zero_fill(path, size, label="Zeroing"):
    """Full (non-quick) format: overwrite the whole partition with zeros."""
    fd = os.open(path, os.O_WRONLY)
    try:
        buf = bytes(CHUNK)
        done = 0
        last = 0
        start = time.time()
        while done < size:
            check_cancel()
            n = os.write(fd, buf[:min(CHUNK, size - done)])
            done += n
            if done - last >= 128 * MiB:
                os.fsync(fd)
                last = done
                report_rate(label, done, size, start)
        os.fsync(fd)
    finally:
        os.close(fd)


def report_rate(label, done, total, start):
    elapsed = max(time.time() - start, 0.001)
    rate = done / elapsed
    remain = (total - done) / rate if rate > 0 else 0
    status(f"{label}: {done * 100 / total:.1f}% "
           f"({rate / MiB:.1f} MB/s, {int(remain // 60)}:{int(remain % 60):02d} left)")
    progress(done / total)


def device_size(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# Bad block check
# --------------------------------------------------------------------------

def check_bad_blocks(dev, passes):
    patterns = {1: ["0xaa"], 2: ["0xaa", "0x55"], 3: ["0xaa", "0x55", "0xff"],
                4: ["0xaa", "0x55", "0xff", "0x00"]}[max(1, min(4, passes))]
    cmd = ["badblocks", "-w", "-s", "-b", "4096", "-c", "1024"]
    for p in patterns:
        cmd += ["-t", p]
    cmd.append(dev)
    log("$ " + " ".join(cmd))
    phases = len(patterns) * 2
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL)
    phase = -1
    buf = b""
    bad_total = None
    rx_pct = re.compile(rb"(\d+\.\d+)% done")
    rx_err = re.compile(rb"\((\d+)/(\d+)/(\d+) errors\)")
    try:
        while True:
            if cancel_event.is_set():
                proc.terminate()
                proc.wait()
                raise Cancelled()
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(256)
            if not chunk:
                break
            buf += chunk
            # Count phase transitions as they stream in.
            while True:
                idx_w = buf.find(b"Testing with pattern")
                idx_r = buf.find(b"Reading and comparing")
                idxs = [i for i in (idx_w, idx_r) if i >= 0]
                if not idxs:
                    break
                i = min(idxs)
                phase += 1
                kw = b"Testing with pattern" if i == idx_w else b"Reading and comparing"
                line_end = buf.find(b":", i)
                log(buf[i:line_end if line_end > 0 else i + 40].decode(errors="replace"))
                buf = buf[i + len(kw):]
            pct = rx_pct.findall(buf)
            if pct:
                frac = float(pct[-1]) / 100
                overall = (max(phase, 0) + frac) / phases
                status(f"Bad blocks check: pass {max(phase, 0) // 2 + 1}/{len(patterns)} "
                       f"{'write' if max(phase, 0) % 2 == 0 else 'read'} {frac * 100:.1f}%")
                progress(overall)
            errs = rx_err.findall(buf)
            if errs:
                bad_total = sum(int(x) for x in errs[-1])
            if len(buf) > 8192:
                buf = buf[-1024:]
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        raise JobError("badblocks failed")
    if bad_total:
        log(f"WARNING: {bad_total} bad block error(s) found! This drive is unreliable.")
        raise JobError(f"Device has {bad_total} bad block error(s)")
    log("Bad blocks check: no errors found.")


# --------------------------------------------------------------------------
# DD mode
# --------------------------------------------------------------------------

def write_image_dd(job, dev, dev_size):
    image = job["image"]
    compression = job.get("compression")
    src_size = os.path.getsize(image)
    if not compression and src_size > dev_size:
        raise JobError(f"Image ({src_size} bytes) is larger than the device ({dev_size} bytes)")

    wipe_device(dev, dev_size)
    status("Writing image...")
    raw = open(image, "rb")
    if compression == "gz":
        src = gzip.GzipFile(fileobj=raw)
    elif compression == "xz":
        src = lzma.LZMAFile(raw)
    elif compression == "bz2":
        src = bz2.BZ2File(raw)
    else:
        src = raw

    hasher = hashlib.sha256() if job.get("verify") else None
    fd = os.open(dev, os.O_WRONLY | os.O_EXCL)
    written = 0
    since_sync = 0
    start = time.time()
    try:
        while True:
            check_cancel()
            data = src.read(CHUNK)
            if not data:
                break
            if written + len(data) > dev_size:
                raise JobError("Decompressed image is larger than the device")
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            if hasher:
                hasher.update(data)
            written += len(data)
            since_sync += len(data)
            if since_sync >= 64 * MiB:
                os.fsync(fd)
                since_sync = 0
                report_rate("Writing", raw.tell(), src_size, start)
        status("Flushing buffers to device...")
        progress(-1)
        os.fsync(fd)
    finally:
        os.close(fd)
        src.close()
    log(f"Wrote {written} bytes in {time.time() - start:.0f}s")

    if hasher:
        verify_device(dev, written, hasher.hexdigest())
    reread_partitions(dev)


def verify_device(dev, length, expected):
    status("Verifying...")
    run(["blockdev", "--flushbufs", dev], check=False, quiet=True)
    try:
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("1\n")
    except OSError:
        pass
    h = hashlib.sha256()
    fd = os.open(dev, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        done = 0
        start = time.time()
        last = 0
        while done < length:
            check_cancel()
            data = os.read(fd, min(CHUNK, length - done))
            if not data:
                break
            h.update(data)
            done += len(data)
            if done - last >= 64 * MiB:
                last = done
                report_rate("Verifying", done, length, start)
    finally:
        os.close(fd)
    if h.hexdigest() != expected:
        raise JobError("Verification FAILED: data on the device does not match the image")
    log(f"Verification OK (sha256 {expected})")


# --------------------------------------------------------------------------
# ISO (file copy) mode
# --------------------------------------------------------------------------

def mount_iso(iso, mnt):
    for fstype in ("udf", "iso9660"):
        proc = run(["mount", "-o", "loop,ro", "-t", fstype, iso, mnt], check=False, quiet=True)
        if proc.returncode == 0:
            # A UDF mount of a hybrid ISO can be empty; fall back if so.
            if os.listdir(mnt):
                log(f"Mounted ISO as {fstype}")
                return
            run(["umount", mnt], check=False, quiet=True)
    raise JobError("Could not mount the ISO image")


def scan_tree(root):
    files = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)        # follows symlinks to files
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                rel = os.path.relpath(full, root)
                files.append((rel, st.st_size))
                total += st.st_size
    return files, total


def find_ci(root, *parts):
    """Case-insensitive path lookup inside the mounted ISO."""
    cur = root
    for part in parts:
        try:
            match = next((e for e in os.listdir(cur) if e.lower() == part.lower()), None)
        except OSError:
            return None
        if match is None:
            return None
        cur = os.path.join(cur, match)
    return cur


def copy_files(src_root, items, targets, total):
    """items: list of (rel, size); targets: function rel -> list of dest roots."""
    done = 0
    start = time.time()
    last = 0
    for rel, size in items:
        for dest_root in targets(rel):
            check_cancel()
            src = os.path.join(src_root, rel)
            dst = os.path.join(dest_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(src, "rb") as fi, open(dst, "wb") as fo:
                while True:
                    check_cancel()
                    data = fi.read(CHUNK)
                    if not data:
                        break
                    fo.write(data)
                    done += len(data)
                    if done - last >= 16 * MiB:
                        last = done
                        report_rate("Copying ISO files", done, total, start)
            if size > 256 * MiB:
                log(f"Copied {rel} ({size / MiB:.0f} MiB)")
    status("Flushing buffers to device (this can take a while)...")
    progress(-1)
    os.sync()


def windows_arch(iso_root):
    if find_ci(iso_root, "efi", "boot", "bootaa64.efi"):
        return "arm64"
    if find_ci(iso_root, "efi", "boot", "bootx64.efi"):
        return "amd64"
    if find_ci(iso_root, "efi", "boot", "bootia32.efi"):
        return "x86"
    return "amd64"


def build_unattend(opts, arch):
    """Rufus-style Windows 11 customization via autounattend.xml."""
    comp_attrs = (f'processorArchitecture="{arch}" publicKeyToken="31bf3856ad364e35" '
                  'language="neutral" versionScope="nonSxS" '
                  'xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" '
                  'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')

    def cmds(paths):
        out = []
        for i, p in enumerate(paths, 1):
            out.append(f'        <RunSynchronousCommand wcm:action="add">\n'
                       f'          <Order>{i}</Order>\n'
                       f'          <Path>{p}</Path>\n'
                       f'        </RunSynchronousCommand>')
        return "\n".join(out)

    sections = []
    if opts.get("bypass_hw"):
        lab = r"reg add HKLM\SYSTEM\Setup\LabConfig /v {} /t REG_DWORD /d 1 /f"
        sections.append(f'''  <settings pass="windowsPE">
    <component name="Microsoft-Windows-Setup" {comp_attrs}>
      <UserData>
        <ProductKey>
          <Key />
        </ProductKey>
      </UserData>
      <RunSynchronous>
{cmds([lab.format(k) for k in ("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck", "BypassStorageCheck", "BypassCPUCheck")])}
      </RunSynchronous>
    </component>
  </settings>''')
    if opts.get("bypass_msa"):
        sections.append(f'''  <settings pass="specialize">
    <component name="Microsoft-Windows-Deployment" {comp_attrs}>
      <RunSynchronous>
{cmds([r"reg add HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE /v BypassNRO /t REG_DWORD /d 1 /f"])}
      </RunSynchronous>
    </component>
  </settings>''')
    if opts.get("no_telemetry") or opts.get("bypass_msa"):
        oobe = []
        if opts.get("no_telemetry"):
            oobe.append("        <ProtectYourPC>3</ProtectYourPC>")
        if opts.get("bypass_msa"):
            oobe.append("        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>")
        sections.append(f'''  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup" {comp_attrs}>
      <OOBE>
{chr(10).join(oobe)}
      </OOBE>
    </component>
  </settings>''')
    if not sections:
        return None
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<unattend xmlns="urn:schemas-microsoft-com:unattend">\n'
            + "\n".join(sections) + "\n</unattend>\n")


def split_wim(src_wim, dest_dir):
    status("Splitting install.wim for FAT32 (wimlib)...")
    progress(-1)
    os.makedirs(dest_dir, exist_ok=True)
    run(["wimlib-imagex", "split", src_wim, os.path.join(dest_dir, "install.swm"), "3800"])


def write_iso_mode(job, dev, dev_size, workdir):
    iso = job["image"]
    scheme = job.get("scheme", "mbr")
    want_fs = job.get("filesystem", "fat32")
    label = job.get("label") or "USB"
    iso_mnt = os.path.join(workdir, "iso")
    os.makedirs(iso_mnt)
    status("Reading ISO...")
    progress(-1)
    mount_iso(iso, iso_mnt)
    mounts = [iso_mnt]
    try:
        files, total = scan_tree(iso_mnt)
        if total > dev_size - 64 * MiB:
            raise JobError("The ISO contents do not fit on this device")
        is_windows = bool(find_ci(iso_mnt, "sources", "install.wim") or
                          find_ci(iso_mnt, "sources", "install.esd") or
                          find_ci(iso_mnt, "bootmgr"))
        big = [(r, s) for r, s in files if s > FAT32_MAX_FILE]
        log(f"ISO: {len(files)} files, {total / MiB:.0f} MiB, windows={is_windows}, "
            f">4GB files={[r for r, _ in big]}")
        if not (find_ci(iso_mnt, "efi", "boot") or is_windows):
            log("WARNING: this ISO has no EFI/BOOT directory; the drive may not be "
                "UEFI-bootable in ISO mode. DD mode is recommended for this image.")

        split = False
        layout = "single"
        if is_windows and want_fs == "ntfs":
            layout = "dual"
        elif big:
            wim = [r for r, _ in big if r.lower() == os.path.join("sources", "install.wim")]
            if is_windows and wim and len(big) == 1 and have("wimlib-imagex"):
                split = True
            elif is_windows:
                layout = "dual"
                log("install.wim is larger than 4 GB and wimlib-imagex is not installed "
                    "(sudo apt install wimtools). Using a FAT32 boot + NTFS data layout.")
            else:
                raise JobError("This ISO contains files larger than 4 GB which FAT32 cannot "
                               "store. Use DD mode instead.")

        unmount_all(dev)
        wipe_device(dev, dev_size)

        if layout == "single":
            fs = "fat32"
            parts = create_partitions(dev, scheme, [(fs, None, True)])
            mkfs(fs, parts[0], label, job.get("cluster", 0))
            dest = os.path.join(workdir, "usb")
            os.makedirs(dest)
            run(["mount", parts[0], dest])
            mounts.append(dest)
            skip = {r for r, _ in big} if split else set()
            items = [(r, s) for r, s in files if r not in skip]
            copy_total = sum(s for _, s in items)
            copy_files(iso_mnt, items, lambda rel: [dest], copy_total)
            if split:
                split_wim(os.path.join(iso_mnt, big[0][0]),
                          os.path.join(dest, os.path.dirname(big[0][0])))
            roots = [dest]
        else:
            # BOOT (FAT32): everything except sources/* (but keep boot.wim).
            def is_boot_file(rel):
                low = rel.lower().replace("\\", "/")
                return not low.startswith("sources/") or low == "sources/boot.wim"
            boot_bytes = sum(s for r, s in files if is_boot_file(r))
            boot_mib = max(512, int(boot_bytes * 1.15 / MiB) + 128)
            log(f"Dual-partition layout: BOOT {boot_mib} MiB FAT32 + INSTALL NTFS")
            parts = create_partitions(dev, scheme, [("fat32", f"{boot_mib}MiB", True),
                                                    ("ntfs", None, False)])
            mkfs("fat32", parts[0], "BOOT", 0)
            mkfs("ntfs", parts[1], label or "INSTALL", job.get("cluster", 0))
            boot_dir = os.path.join(workdir, "boot")
            inst_dir = os.path.join(workdir, "install")
            os.makedirs(boot_dir)
            os.makedirs(inst_dir)
            run(["mount", parts[0], boot_dir])
            mounts.append(boot_dir)
            if run(["mount", "-t", "ntfs-3g", parts[1], inst_dir], check=False).returncode != 0:
                run(["mount", parts[1], inst_dir])
            mounts.append(inst_dir)
            copy_total = total + boot_bytes

            def targets(rel):
                return [boot_dir, inst_dir] if is_boot_file(rel) else [inst_dir]
            copy_files(iso_mnt, files, targets, copy_total)
            roots = [boot_dir, inst_dir]

        if is_windows:
            xml = build_unattend(job.get("win_opts") or {}, windows_arch(iso_mnt))
            if xml:
                for r in roots:
                    with open(os.path.join(r, "autounattend.xml"), "w") as f:
                        f.write(xml)
                log("Added autounattend.xml with selected Windows customizations")
        os.sync()
    finally:
        for m in reversed(mounts):
            run(["umount", m], check=False, quiet=True)


# --------------------------------------------------------------------------
# Compressed file (archive) mode
# --------------------------------------------------------------------------
# Archives are untrusted input, so listing and extracting run in a forked child
# that has dropped root and runs as the user who started Rufix. A crafted
# archive can then at worst write what that user could write anyway.

ARCHIVE_TOOLS = ("7zz", "7z", "7za", "unrar", "bsdtar")
# Characters FAT and exFAT cannot store in file names.
FAT_BAD_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')


def invoking_user():
    uid = int(os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID") or 0)
    try:
        gid = pwd.getpwuid(uid).pw_gid
    except KeyError:
        gid = uid
    return uid, gid


def as_user(fn, *args):
    """Run fn(*args) in a child process with the invoking user's privileges.

    The child may emit progress messages itself; its JSON-serialisable return
    value is passed back through a pipe. Cancelling kills the child and any
    tool it started.
    """
    uid, gid = invoking_user()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            os.setpgid(0, 0)
            if uid:
                os.setgroups([])
                os.setgid(gid)
                os.setuid(uid)
            res = {"ok": True, "v": fn(*args)}
        except BaseException as e:  # noqa: BLE001 - report everything to the parent
            res = {"ok": False, "m": str(e) or e.__class__.__name__}
        try:
            with os.fdopen(w, "w") as f:
                json.dump(res, f)
        finally:
            os._exit(0)
    os.close(w)
    try:
        while True:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            if cancel_event.is_set():
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
                os.waitpid(pid, 0)
                raise Cancelled()
            time.sleep(0.2)
        with os.fdopen(r, "r") as f:
            r = None
            data = f.read()
    finally:
        if r is not None:
            os.close(r)
    res = json.loads(data) if data else {"ok": False, "m": "Archive process exited unexpectedly"}
    if not res.get("ok"):
        raise JobError(res.get("m") or "Archive processing failed")
    return res["v"]


def safe_rel(name, fat_names=False):
    """Archive member name -> safe relative path, or None if it escapes the target."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    if fat_names:
        parts = [FAT_BAD_CHARS.sub("_", p).rstrip(" .") or "_" for p in parts]
    return os.path.join(*parts)


def archive_kind(path):
    if zipfile.is_zipfile(path):
        return "zip"
    try:
        if tarfile.is_tarfile(path):
            return "tar"
    except (OSError, tarfile.TarError):
        pass
    return "external"


def _list_7z(tool, path):
    proc = subprocess.run([tool, "l", "-slt", "-ba", path], capture_output=True, text=True,
                          errors="replace", stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        return None
    entries, cur = [], {}
    for line in proc.stdout.splitlines() + [""]:
        if not line.strip():
            if "Path" in cur:
                entries.append(cur)
            cur = {}
        elif " = " in line:
            k, v = line.split(" = ", 1)
            cur[k.strip()] = v
    return [(e["Path"], int(e.get("Size") or 0), e.get("Attributes", "").startswith("D")
             or e.get("Folder") == "+") for e in entries]


def list_archive(path):
    """Runs as the user. Returns total/largest size, file count and the tool to use."""
    kind = archive_kind(path)
    entries = []  # (name, size, is_dir)
    tool = None
    if kind == "zip":
        with zipfile.ZipFile(path) as z:
            for i in z.infolist():
                if i.flag_bits & 0x1:
                    raise JobError("Password-protected archives are not supported")
                entries.append((i.filename, i.file_size, i.is_dir()))
    elif kind == "tar":
        with tarfile.open(path) as t:
            entries = [(m.name, m.size if m.isfile() else 0, m.isdir()) for m in t.getmembers()
                       if m.isfile() or m.isdir()]
    else:
        tried = []
        for name in ARCHIVE_TOOLS:
            if not have(name) or (name == "unrar" and not path.lower().endswith(".rar")):
                continue
            tried.append(name)
            if name in ("7zz", "7z", "7za"):
                listed = _list_7z(name, path)
                if listed is not None:
                    tool, entries = name, listed
                    break
            else:
                tool = name  # no size listing; checked by free space while extracting
                break
        if not tool and tried:
            raise JobError(f"This file is not a valid or supported archive "
                           f"({', '.join(tried)} could not open it).")
        if not tool:
            raise JobError("Cannot open this archive. Install 7-Zip to extract 7z and RAR "
                           "files (sudo apt install 7zip, or p7zip-full + unrar).")
    files = [(n, s) for n, s, d in entries if not d]
    unsafe = [n for n, _, _ in entries if safe_rel(n) is None]
    return {"kind": kind, "tool": tool, "count": len(files),
            "total": sum(s for _, s in files) if (kind != "external" or entries) else None,
            "largest": max((s for _, s in files), default=0), "unsafe": unsafe[:5]}


def _extract_stream(members, open_member, dest, total, fat_names):
    """members: list of (name, size, is_dir, handle)."""
    done = 0
    last = 0
    start = time.time()
    for name, size, is_dir, handle in members:
        rel = safe_rel(name, fat_names)
        if rel is None:
            continue
        target = os.path.join(dest, rel)
        if is_dir:
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open_member(handle) as fi, open(target, "wb") as fo:
            while True:
                data = fi.read(CHUNK)
                if not data:
                    break
                fo.write(data)
                done += len(data)
                if total and done - last >= 16 * MiB:
                    last = done
                    report_rate("Extracting", min(done, total), total, start)


def _extract_external(tool, path, dest, total):
    if tool in ("7zz", "7z", "7za"):
        cmd = [tool, "x", "-y", "-bd", f"-o{dest}", path]
    elif tool == "unrar":
        cmd = ["unrar", "x", "-o+", "-y", path, dest + "/"]
    else:
        cmd = ["bsdtar", "-x", "-f", path, "-C", dest]
    log("$ " + " ".join(cmd))
    used0 = shutil.disk_usage(dest).used
    start = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, errors="replace")
    out = []
    reader = threading.Thread(target=lambda: out.extend(proc.stdout), daemon=True)
    reader.start()
    while proc.poll() is None:
        time.sleep(0.5)
        if total:
            done = max(0, shutil.disk_usage(dest).used - used0)
            report_rate("Extracting", min(done, total), total, start)
        else:
            progress(-1)
    reader.join(timeout=5)
    for line in [x.rstrip() for x in out if x.strip()][-15:]:
        log("  " + line)
    if proc.returncode != 0:
        raise JobError(f"{tool} failed to extract the archive (exit code {proc.returncode})")


def extract_archive(path, dest, info, fat_names):
    """Runs as the user."""
    status("Extracting...")
    progress(0 if info["total"] else -1)
    if info["kind"] == "zip":
        with zipfile.ZipFile(path) as z:
            members = [(i.filename, i.file_size, i.is_dir(), i) for i in z.infolist()
                       if not stat.S_ISLNK(i.external_attr >> 16)]
            _extract_stream(members, z.open, dest, info["total"], fat_names)
    elif info["kind"] == "tar":
        with tarfile.open(path) as t:
            members = [(m.name, m.size, m.isdir(), m) for m in t.getmembers()
                       if m.isfile() or m.isdir()]
            _extract_stream(members, t.extractfile, dest, info["total"], fat_names)
    else:
        _extract_external(info["tool"], path, dest, info["total"])
    return True


def job_extract(job, dev, dev_size, workdir):
    archive = job["archive"]
    fs = job["filesystem"]
    status("Reading archive...")
    progress(-1)
    info = as_user(list_archive, archive)
    log(f"Archive: {info['kind']} ({info['tool'] or 'built-in'}), {info['count']} files, "
        + (f"{info['total'] / MiB:.0f} MiB uncompressed" if info["total"] is not None
           else "uncompressed size unknown"))
    # Check everything that can fail before the drive is wiped.
    if info["unsafe"]:
        raise JobError("The archive contains unsafe paths (absolute or '..'): "
                       + ", ".join(info["unsafe"]))
    if fs in ("fat32", "fat16") and info["largest"] > FAT32_MAX_FILE:
        raise JobError("The archive contains files larger than 4 GB, which FAT32 cannot "
                       "store. Choose exFAT or NTFS instead.")
    if info["total"] and info["total"] > dev_size - 64 * MiB:
        raise JobError(f"The extracted files ({info['total'] / MiB:.0f} MiB) do not fit "
                       "on this device")

    job_format(job, dev, dev_size)
    part = part_path(dev, 1)
    dest = os.path.join(workdir, "usb")
    os.makedirs(dest)
    # mkdtemp creates workdir as 0700 root; the unprivileged extractor needs
    # to pass through it to reach the mounted drive (no listing rights).
    os.chmod(workdir, 0o711)
    uid, gid = invoking_user()
    if fs == "ntfs":
        ok = run(["mount", "-t", "ntfs-3g", "-o", f"uid={uid},gid={gid}", part, dest],
                 check=False).returncode == 0
        if not ok:
            run(["mount", "-o", f"uid={uid},gid={gid}", part, dest])
    elif fs == "ext4":
        run(["mount", part, dest])
        os.chown(dest, uid, gid)
    else:
        run(["mount", "-o", f"uid={uid},gid={gid},umask=022", part, dest])
    try:
        as_user(extract_archive, archive, dest, info, fs in ("fat32", "fat16", "exfat"))
        status("Flushing buffers to device (this can take a while)...")
        progress(-1)
        os.sync()
    finally:
        run(["umount", dest], check=False, quiet=True)
    log("Archive extracted")


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

def job_format(job, dev, dev_size):
    fs = job["filesystem"]
    wipe_device(dev, dev_size)
    parts = create_partitions(dev, job.get("scheme", "mbr"), [(fs, None, False)])
    part = parts[0]
    if not job.get("quick", True):
        zero_fill(part, device_size(part), "Full format (zeroing)")
    mkfs(fs, part, job.get("label", ""), job.get("cluster", 0))
    settle()


def main():
    if os.geteuid() != 0:
        emit(t="done", ok=False, m="The helper must run as root")
        return 1
    line = sys.stdin.readline()
    try:
        job = json.loads(line)
    except json.JSONDecodeError:
        emit(t="done", ok=False, m="Invalid job description")
        return 1
    threading.Thread(target=_stdin_watcher, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: cancel_event.set())

    dev = job.get("device", "")
    workdir = tempfile.mkdtemp(prefix="rufix-")
    try:
        dev_size = validate_device(dev, bool(job.get("allow_usb_hdd")))
        log(f"Target: {dev} ({dev_size} bytes), action: {job.get('action')}")
        unmount_all(dev)
        if job.get("badblocks"):
            check_bad_blocks(dev, int(job["badblocks"]))
        action = job.get("action")
        if action == "format":
            job_format(job, dev, dev_size)
        elif action == "dd":
            write_image_dd(job, dev, dev_size)
        elif action == "iso":
            write_iso_mode(job, dev, dev_size, workdir)
        elif action == "extract":
            job_extract(job, dev, dev_size, workdir)
        else:
            raise JobError(f"Unknown action {action}")
        settle()
        progress(1.0)
        emit(t="done", ok=True, m="READY")
        return 0
    except Cancelled:
        log("Operation cancelled by user. The drive is probably unusable until reformatted.")
        emit(t="done", ok=False, m="Cancelled")
        return 2
    except (JobError, OSError) as e:
        log(f"ERROR: {e}")
        emit(t="done", ok=False, m=str(e))
        return 1
    finally:
        # Never rmtree: a mount point that failed to unmount would be wiped.
        for sub in ("iso", "usb", "boot", "install"):
            try:
                os.rmdir(os.path.join(workdir, sub))
            except OSError:
                pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
