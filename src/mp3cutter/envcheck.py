"""``mp3cut --check-env``: installation / PATH / FFmpeg diagnostics."""

from __future__ import annotations

import importlib
import os
import sys
from typing import List

from mp3cutter import __version__
from mp3cutter import platform_info as pi
from mp3cutter.ffmpeg import find_tool, has_mp3_encoder, install_hint, tool_origin, tool_version


def _row(label: str, value: object) -> None:
    print(f"  {label:<26}{value}")


def run_check() -> int:
    issues: List[str] = []
    system = pi.os_name()
    exe = pi.installed_executable()
    on_path = pi.which_mp3cut()

    print(f"\nmp3cut {__version__} environment check\n")
    print("System")
    _row("Operating system", f"{system} {pi.os_version()}")
    _row("Architecture", f"{pi.architecture()} (Python {pi.python_bits()}-bit)")
    _row("Shell", pi.detect_shell())

    print("\nPython")
    _row("Version", sys.version.split()[0])
    _row("Executable", sys.executable)
    _row("Installation dir", sys.prefix)
    _row("Virtual environment", "yes" if pi.in_venv() else "no")
    _row("User scripts dir", pi.user_scripts_dir())

    print("\nmp3cut")
    _row("Package location", pi.package_location())
    _row("Installed executable", exe or "NOT FOUND")
    _row("Resolved via PATH", on_path or "NOT ON PATH")
    if exe is None:
        issues.append(f"mp3cut is not installed for this Python. From the mp3cut folder run:\n"
                      f"      \"{sys.executable}\" -m pip install --user .")
        path_ok = False
    else:
        path_ok = pi.dir_in_path_value(exe.parent)
        _row("PATH status", f"OK ({exe.parent} is on PATH)" if path_ok else f"MISSING ({exe.parent})")
        if not path_ok:
            if system == "Windows":
                issues.append(
                    f"{exe.parent} is not on PATH. Fix it with:\n"
                    f"      \"{sys.executable}\" -m mp3cutter.pathsetup install\n"
                    "    then open a NEW terminal window.")
            else:
                issues.append(
                    f"{exe.parent} is not on PATH. Fix it with:\n"
                    f"      \"{sys.executable}\" -m mp3cutter.pathsetup install\n"
                    f"    then open a new terminal; or for this terminal only:\n"
                    f"      export PATH=\"{exe.parent}:$PATH\"")
        elif on_path and os.path.normcase(os.path.realpath(on_path)) != os.path.normcase(os.path.realpath(str(exe))):
            issues.append(f"A different mp3cut is found first on PATH: {on_path}\n"
                          f"    Expected: {exe}. Remove the other copy or move {exe.parent} earlier in PATH.")

    print("\nFFmpeg")
    from mp3cutter.prereqs import managed_dir
    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    _row("ffmpeg", f"{ffmpeg}  (version {tool_version(ffmpeg)}, {tool_origin(ffmpeg)})" if ffmpeg else "NOT FOUND")
    _row("ffprobe", f"{ffprobe}  (version {tool_version(ffprobe)}, {tool_origin(ffprobe)})" if ffprobe else "NOT FOUND")
    _row("Private FFmpeg folder", f"{managed_dir()}" + ("" if managed_dir().exists() else "  (not installed)"))
    if ffmpeg:
        enc = has_mp3_encoder(ffmpeg)
        _row("MP3 encoder (libmp3lame)", "available" if enc else "MISSING")
        if not enc:
            issues.append("Your FFmpeg has no MP3 encoder. " + install_hint())
    if not ffmpeg or not ffprobe:
        issues.append("FFmpeg is missing. " + install_hint())

    print("\nPython dependencies")
    for mod in ("mp3cutter.core", "mp3cutter.cli", "json", "subprocess", "argparse", "sysconfig"):
        try:
            importlib.import_module(mod)
            _row(mod, "OK")
        except Exception as exc:  # pragma: no cover
            _row(mod, f"FAILED ({exc})")
            issues.append(f"Cannot import {mod}: reinstall mp3cut")
    _row("Third-party packages", "none required")

    print()
    if issues:
        print("Problems found:")
        for i in issues:
            print(f"  - {i}")
        print()
        return 1
    print("Everything looks good. Try:  mp3cut --input song.mp3 --start 01:30 --end 02:45\n")
    return 0
