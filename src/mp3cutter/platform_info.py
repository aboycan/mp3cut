"""Platform, Python and install-location detection. No paths are hardcoded:
everything is derived from the running interpreter and the environment."""

from __future__ import annotations

import os
import platform
import shutil
import struct
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import List, Optional

EXE_NAME = "mp3cut.exe" if os.name == "nt" else "mp3cut"


# --------------------------------------------------------------------------- OS
def os_name() -> str:
    s = platform.system()
    return {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(s, s or "Unknown")


def os_version() -> str:
    system = os_name()
    if system == "macOS":
        return platform.mac_ver()[0] or platform.release()
    if system == "Windows":
        try:
            build = sys.getwindowsversion().build  # type: ignore[attr-defined]
        except AttributeError:
            return platform.release()
        major = "11" if build >= 22000 else platform.release()
        return f"{major} (build {build})"
    if system == "Linux":
        return linux_distro_name() or platform.release()
    return platform.release()


def _os_release() -> dict:
    data = {}
    for candidate in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(candidate, encoding="utf-8") as fh:
                for line in fh:
                    key, sep, val = line.strip().partition("=")
                    if sep:
                        data[key] = val.strip().strip('"').strip("'")
            break
        except OSError:
            continue
    return data


def linux_distro_id() -> Optional[str]:
    if os_name() != "Linux":
        return None
    return _os_release().get("ID") or None


def linux_distro_name() -> Optional[str]:
    if os_name() != "Linux":
        return None
    rel = _os_release()
    return rel.get("PRETTY_NAME") or rel.get("NAME")


# ------------------------------------------------------------------ architecture
def architecture() -> str:
    m = (platform.machine() or "").lower()
    if m in ("x86_64", "amd64", "x64"):
        arch = "x86_64"
    elif m in ("arm64", "aarch64", "armv8l", "armv8b"):
        arch = "ARM64"
    elif m in ("i386", "i486", "i586", "i686", "x86"):
        arch = "x86"
    else:
        arch = platform.machine() or "unknown"
    # x86_64 Python running under Rosetta on Apple Silicon
    if os_name() == "macOS" and arch == "x86_64" and _rosetta():
        return "ARM64 (Python running as x86_64 under Rosetta)"
    # x64 Python under emulation on Windows ARM64
    if os_name() == "Windows" and os.environ.get("PROCESSOR_ARCHITEW6432", "").upper() == "ARM64":
        return "ARM64 (Python running emulated)"
    if os_name() == "Windows" and os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() == "ARM64" and arch != "ARM64":
        return "ARM64 (Python running emulated)"
    return arch


def _rosetta() -> bool:
    try:
        out = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, timeout=5).stdout.strip()
        return out == "1"
    except (OSError, subprocess.SubprocessError):
        return False


def python_bits() -> int:
    return struct.calcsize("P") * 8


# ------------------------------------------------------------------------ shell
def detect_shell() -> str:
    """Return the user's login shell name (zsh, bash, fish, ...) or 'cmd'/'powershell' on Windows."""
    if os.name == "nt":
        return "cmd / PowerShell"
    shell = os.environ.get("SHELL")
    if not shell:
        try:
            import pwd
            shell = pwd.getpwuid(os.getuid()).pw_shell
        except (ImportError, KeyError, OSError):
            shell = ""
    name = Path(shell).name if shell else ""
    if not name:
        name = "zsh" if os_name() == "macOS" else "bash"
    return name


# ----------------------------------------------------------------------- python
def in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _user_scheme() -> str:
    get_pref = getattr(sysconfig, "get_preferred_scheme", None)
    if get_pref is not None:
        try:
            return get_pref("user")
        except Exception:  # pragma: no cover - defensive
            pass
    if os.name == "nt":
        return "nt_user"
    if sys.platform == "darwin" and getattr(sys, "_framework", ""):
        return "osx_framework_user"
    return "posix_user"


def user_scripts_dir() -> Path:
    """Directory where ``pip install --user`` puts console scripts for this interpreter."""
    return Path(sysconfig.get_path("scripts", _user_scheme()))


def default_scripts_dir() -> Path:
    """Scripts dir for a non-user install (the interpreter's or the active venv's)."""
    return Path(sysconfig.get_path("scripts"))


def installed_executable() -> Optional[Path]:
    """Locate the ``mp3cut`` launcher actually written by pip, via the package RECORD.

    This is authoritative even when the interpreter's user base is redirected
    (e.g. Microsoft Store Python) or a venv is used.
    """
    try:
        from importlib import metadata
        dist = metadata.distribution("mp3cut")
    except Exception:
        dist = None
    if dist is not None:
        for f in dist.files or []:
            if f.name.lower() == EXE_NAME.lower():
                try:
                    p = Path(str(dist.locate_file(f))).resolve()
                except OSError:
                    continue
                if p.is_file():
                    return p
    for d in (user_scripts_dir(), default_scripts_dir()):
        p = d / EXE_NAME
        if p.is_file():
            return p
    return None


def package_location() -> Path:
    return Path(__file__).resolve().parent


# ------------------------------------------------------------------------- PATH
def _norm(entry: str) -> str:
    e = os.path.expandvars(os.path.expanduser(entry.strip().strip('"')))
    e = os.path.normpath(e) if e else e
    e = e.rstrip("\\/") or e
    return os.path.normcase(e)


def path_entries(path_value: Optional[str] = None) -> List[str]:
    value = os.environ.get("PATH", "") if path_value is None else path_value
    return [e for e in value.split(os.pathsep) if e.strip()]


def dir_in_path_value(directory: Path, path_value: Optional[str] = None) -> bool:
    target = _norm(str(directory))
    try:
        target_real = os.path.normcase(os.path.realpath(str(directory)))
    except OSError:
        target_real = target
    for e in path_entries(path_value):
        n = _norm(e)
        if n == target:
            return True
        try:
            if os.path.normcase(os.path.realpath(n)) == target_real:
                return True
        except OSError:
            pass
    return False


def which_mp3cut(path_value: Optional[str] = None) -> Optional[str]:
    return shutil.which("mp3cut", path=path_value)
