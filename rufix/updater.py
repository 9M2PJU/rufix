"""Check GitHub for new Rufix releases and install them.

The check and download run as the user; only the final install step is run
through pkexec. Downloads are verified against the SHA-256 digest that the
GitHub API publishes for each release asset.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import __version__

REPO = "thuutien/rufix"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
INSTALL_DIR = "/usr/lib/rufix"
TIMEOUT = 15


@dataclass
class Release:
    version: str
    url: str
    notes: str = ""
    # asset name -> (download url, sha256 hex or "")
    assets: Dict[str, Tuple[str, str]] = field(default_factory=dict)


def version_tuple(v: str) -> Tuple[int, ...]:
    parts = []
    for p in v.lstrip("vV").split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(latest: str, current: str = __version__) -> bool:
    return version_tuple(latest) > version_tuple(current)


def _open(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": f"rufix/{__version__}",
        "Accept": "application/vnd.github+json",
    })
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def fetch_latest() -> Release:
    """Return the latest published release. Raises OSError/ValueError on failure."""
    with _open(API_LATEST) as resp:
        data = json.load(resp)
    tag = data.get("tag_name") or ""
    if not tag:
        raise ValueError("No release found")
    rel = Release(version=tag.lstrip("vV"), url=data.get("html_url") or RELEASES_PAGE,
                  notes=data.get("body") or "")
    for a in data.get("assets", []):
        digest = a.get("digest") or ""
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
        rel.assets[a["name"]] = (a["browser_download_url"], sha)
    return rel


def _owned_by(cmd: List[str]) -> bool:
    if not shutil.which(cmd[0]):
        return False
    return subprocess.run(cmd, capture_output=True).returncode == 0


def install_method() -> str:
    """How this copy of Rufix was installed.

    "deb"     - Debian package, can be upgraded with apt
    "tarball" - install.sh into /usr/lib/rufix, can be upgraded the same way
    "package" - owned by another package manager (rpm, pacman/AUR)
    "flatpak" - Flatpak bundle; the user installs the new bundle
    "manual"  - running from source or an AppImage; the user updates it
    """
    if os.path.exists("/.flatpak-info"):
        return "flatpak"
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.environ.get("APPIMAGE") or here != INSTALL_DIR:
        return "manual"
    if shutil.which("dpkg-query"):
        status = subprocess.run(["dpkg-query", "-W", "-f=${Status}", "rufix"],
                                capture_output=True, text=True).stdout
        if status.endswith(" installed"):
            return "deb"
    if _owned_by(["rpm", "-q", "rufix"]) or _owned_by(["pacman", "-Qq", "rufix"]):
        return "package"
    return "tarball"


def asset_for(rel: Release, method: str) -> Optional[str]:
    if method == "deb":
        name = f"rufix_{rel.version}_all.deb"
    elif method == "tarball":
        name = f"rufix-{rel.version}.tar.gz"
    else:
        return None
    return name if name in rel.assets else None


def download(rel: Release, name: str, dest_dir: str, progress=None) -> str:
    """Download a release asset and verify its SHA-256. Returns the file path."""
    url, sha = rel.assets[name]
    if not url.startswith(DOWNLOAD_PREFIX):
        raise ValueError(f"Unexpected download location: {url}")
    if not sha:
        raise ValueError(f"GitHub did not publish a checksum for {name}")
    path = os.path.join(dest_dir, name)
    h = hashlib.sha256()
    with _open(url) as resp, open(path, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    if h.hexdigest() != sha.lower():
        os.unlink(path)
        raise ValueError(f"Checksum mismatch for {name}; the download was discarded")
    return path


def install_command(method: str, path: str, dest_dir: str) -> List[str]:
    """Prepare the downloaded asset and return the privileged install command."""
    if method == "deb":
        return ["pkexec", "apt-get", "install", "-y", path]
    if method == "tarball":
        with tarfile.open(path) as tar:
            members = tar.getmembers()
            for m in members:
                if m.name.startswith("/") or ".." in m.name.split("/") or \
                        not (m.isfile() or m.isdir()):
                    raise ValueError(f"Unsafe entry in archive: {m.name}")
            tar.extractall(dest_dir, members=members)
        top = os.path.basename(path)[:-len(".tar.gz")]
        script = os.path.join(dest_dir, top, "install.sh")
        if not os.path.isfile(script):
            raise ValueError("install.sh not found in the release archive")
        return ["pkexec", "/bin/sh", script]
    raise ValueError(f"Cannot install automatically for method '{method}'")
