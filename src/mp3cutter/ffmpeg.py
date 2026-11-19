"""Locating FFmpeg / FFprobe and reporting their capabilities."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from mp3cutter import platform_info as pi


def find_tool(name: str) -> Optional[str]:
    """Return the path of ``ffmpeg`` / ``ffprobe``.

    Order: ``MP3CUT_FFMPEG`` / ``MP3CUT_FFPROBE`` override, then mp3cut's own
    private copy (installed with ``mp3cut --install-ffmpeg``), then PATH.
    """
    override = os.environ.get(f"MP3CUT_{name.upper()}")
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return str(p)
    from mp3cutter.prereqs import managed_tool
    managed = managed_tool(name)
    if managed is not None:
        return str(managed)
    return shutil.which(name)


def tool_origin(path: Optional[str]) -> str:
    from mp3cutter.prereqs import is_managed
    if not path:
        return "missing"
    if os.environ.get("MP3CUT_FFMPEG") == path or os.environ.get("MP3CUT_FFPROBE") == path:
        return "from MP3CUT_* variable"
    return "mp3cut private copy" if is_managed(path) else "system"


def tool_version(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        proc = subprocess.run(
            [path, "-hide_banner", "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = (proc.stdout or "").splitlines()[:1]
    if not first:
        return None
    m = re.search(r"version\s+(\S+)", first[0])
    return m.group(1) if m else first[0].strip()


def has_mp3_encoder(ffmpeg: Optional[str]) -> bool:
    if not ffmpeg:
        return False
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "libmp3lame" in (proc.stdout or "")


def install_hint() -> str:
    """How to get FFmpeg: mp3cut's own download first, the system package manager as an alternative."""
    return "Run:  mp3cut --install-ffmpeg   (downloads a private copy, no admin needed)\n" \
           "  or: " + system_install_hint()


def system_install_hint() -> str:
    """Platform-specific instructions for installing FFmpeg system-wide."""
    system = pi.os_name()
    if system == "macOS":
        return "brew install ffmpeg"
    if system == "Windows":
        return "winget install Gyan.FFmpeg   (then open a new terminal)"
    distro = pi.linux_distro_id()
    family = {
        "ubuntu": "sudo apt install ffmpeg", "debian": "sudo apt install ffmpeg",
        "linuxmint": "sudo apt install ffmpeg", "pop": "sudo apt install ffmpeg",
        "fedora": "sudo dnf install ffmpeg   (enable RPM Fusion first)",
        "rhel": "sudo dnf install ffmpeg   (enable EPEL + RPM Fusion first)",
        "centos": "sudo dnf install ffmpeg   (enable EPEL + RPM Fusion first)",
        "rocky": "sudo dnf install ffmpeg   (enable EPEL + RPM Fusion first)",
        "almalinux": "sudo dnf install ffmpeg   (enable EPEL + RPM Fusion first)",
        "arch": "sudo pacman -S ffmpeg", "manjaro": "sudo pacman -S ffmpeg",
        "opensuse": "sudo zypper install ffmpeg", "opensuse-leap": "sudo zypper install ffmpeg",
        "opensuse-tumbleweed": "sudo zypper install ffmpeg", "alpine": "sudo apk add ffmpeg",
    }
    return family.get(distro or "", "install the 'ffmpeg' package with your distribution's package manager")
