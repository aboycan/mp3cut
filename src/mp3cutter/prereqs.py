"""Prerequisites bundled with mp3cut: a private, user-level copy of FFmpeg + FFprobe.

If FFmpeg isn't already available, mp3cut downloads static builds into its own
folder (no admin / sudo, nothing system-wide is changed):

    macOS    ~/Library/Application Support/mp3cut/ffmpeg/
    Linux    $XDG_DATA_HOME/mp3cut/ffmpeg/   (default ~/.local/share/mp3cut/ffmpeg/)
    Windows  %LOCALAPPDATA%\\mp3cut\\ffmpeg\\

Sources (static builds, SHA-256 verified when the publisher provides a checksum):
    macOS / Linux : https://ffmpeg.martin-riedl.de   (Intel + ARM64)
    Windows       : https://github.com/BtbN/FFmpeg-Builds  (x64 + ARM64)

Mirrors can be used by setting MP3CUT_FFMPEG_URL (and MP3CUT_FFPROBE_URL when
ffprobe is a separate archive).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from mp3cutter import __version__
from mp3cutter import platform_info as pi

TOOLS = ("ffmpeg", "ffprobe")
EXE = ".exe" if os.name == "nt" else ""
USER_AGENT = f"mp3cut/{__version__} (+python-urllib)"

RIEDL_URL = "https://ffmpeg.martin-riedl.de/redirect/latest/{os}/{arch}/release/{tool}.zip"
BTBN_BASE = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
BTBN_ZIP = "ffmpeg-master-latest-{target}-gpl.zip"

Logger = Callable[[str], None]


class PrereqError(Exception):
    pass


def _print(msg: str) -> None:
    print(msg, file=sys.stderr)


# ----------------------------------------------------------------- locations
def managed_dir() -> Path:
    system = pi.os_name()
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    elif system == "macOS":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / "mp3cut" / "ffmpeg"


def managed_tool(name: str) -> Optional[Path]:
    p = managed_dir() / (name + EXE)
    return p if p.is_file() else None


def is_managed(path: Optional[str]) -> bool:
    if not path:
        return False
    try:
        return Path(path).resolve().parent == managed_dir().resolve()
    except OSError:
        return False


# --------------------------------------------------------------------- plan
@dataclass
class Archive:
    url: str
    tools: Tuple[str, ...]
    checksum: str          # "sidecar" (<final-url>.sha256), "btbn" (checksums.sha256), or "optional"


def _arch() -> Optional[str]:
    if os.name == "nt":
        real = (os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper()
        if real == "ARM64":
            return "arm64"
        if real in ("AMD64", "X64"):
            return "x64"
    m = (platform.machine() or "").lower()
    if pi.os_name() == "macOS" and m == "x86_64" and pi._rosetta():
        return "arm64"                       # Apple Silicon: prefer native binaries even under Rosetta
    if m in ("x86_64", "amd64", "x64"):
        return "x64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return None


def download_plan() -> List[Archive]:
    ff_override = os.environ.get("MP3CUT_FFMPEG_URL")
    if ff_override:
        probe_override = os.environ.get("MP3CUT_FFPROBE_URL")
        if probe_override:
            return [Archive(ff_override, ("ffmpeg",), "optional"), Archive(probe_override, ("ffprobe",), "optional")]
        return [Archive(ff_override, TOOLS, "optional")]

    system, arch = pi.os_name(), _arch()
    if arch is None:
        raise PrereqError(f"no prebuilt FFmpeg for CPU '{platform.machine()}'. Install FFmpeg manually.")
    if system in ("macOS", "Linux"):
        os_key = "macos" if system == "macOS" else "linux"
        arch_key = "arm64" if arch == "arm64" else "amd64"
        return [Archive(RIEDL_URL.format(os=os_key, arch=arch_key, tool=t), (t,), "sidecar") for t in TOOLS]
    if system == "Windows":
        target = "winarm64" if arch == "arm64" else "win64"
        return [Archive(BTBN_BASE + BTBN_ZIP.format(target=target), TOOLS, "btbn")]
    raise PrereqError(f"automatic FFmpeg download is not available for {system}")


def approx_size() -> str:
    return "about 160 MB" if pi.os_name() == "Windows" else "about 60-100 MB"


# ----------------------------------------------------------------- download
def _progress(done: int, total: int, label: str) -> None:
    if not sys.stderr.isatty():
        return
    mb = done / 1_048_576
    if total:
        frac = min(done / total, 1.0)
        bar = "#" * int(frac * 30)
        sys.stderr.write(f"\r  {label} [{bar:<30}] {mb:6.1f} / {total / 1_048_576:.1f} MB")
    else:
        sys.stderr.write(f"\r  {label} {mb:6.1f} MB")
    sys.stderr.flush()


def _is_ssl_problem(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(exc)


def _curl(url: str, dest: Path) -> str:
    curl = shutil.which("curl")
    if not curl:
        raise PrereqError("HTTPS certificate check failed and curl is not available. "
                          "On macOS run 'Install Certificates.command' from your Python folder.")
    args = [curl, "-fL", "--retry", "2", "-A", USER_AGENT, "-o", str(dest), "-w", "%{url_effective}"]
    args += ["--progress-bar"] if sys.stderr.isatty() else ["-sS"]
    proc = subprocess.run(args + [url], stdout=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise PrereqError(f"download failed ({url}), curl exit code {proc.returncode}")
    return proc.stdout.strip() or url


def download(url: str, dest: Path, label: str = "Downloading") -> str:
    """Download url to dest; returns the final URL after redirects."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as fh:
            final = resp.geturl()
            total = int(resp.headers.get("Content-Length") or 0)
            done, last = 0, 0.0
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if time.monotonic() - last > 0.2:
                    _progress(done, total, label)
                    last = time.monotonic()
            _progress(done, total, label)
        if sys.stderr.isatty():
            sys.stderr.write("\n")
        return final
    except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
        if sys.stderr.isatty():
            sys.stderr.write("\n")
        if _is_ssl_problem(exc):
            return _curl(url, dest)            # e.g. python.org macOS Python without certificates
        raise PrereqError(f"download failed ({url}): {getattr(exc, 'reason', exc)}") from exc


def _fetch_text(url: str) -> Optional[str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read(1_000_000).decode("utf-8", "replace")
    except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
        curl = shutil.which("curl")
        if _is_ssl_problem(exc) and curl:
            p = subprocess.run([curl, "-fsSL", url], stdout=subprocess.PIPE, text=True)
            return p.stdout if p.returncode == 0 else None
        return None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _expected_hash(archive: Archive, final_url: str) -> Optional[str]:
    if archive.checksum == "btbn":
        text = _fetch_text(BTBN_BASE + "checksums.sha256")
        name = final_url.rsplit("/", 1)[-1]
        for line in (text or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                return parts[0].lower()
        return None
    # sidecar: the publisher serves <file>.sha256 next to the real (post-redirect) file
    for candidate in dict.fromkeys([final_url + ".sha256", archive.url + ".sha256"]):
        text = _fetch_text(candidate)
        m = re.search(r"\b[0-9a-fA-F]{64}\b", text or "")
        if m:
            return m.group(0).lower()
    return None


def _verify(archive: Archive, path: Path, final_url: str, log: Logger) -> None:
    expected = _expected_hash(archive, final_url)
    actual = sha256(path)
    if expected is None:
        if archive.checksum != "optional":
            log("  Note: publisher checksum not available; the binary will be test-run instead.")
        return
    if expected != actual:
        raise PrereqError(f"checksum mismatch for {final_url.rsplit('/', 1)[-1]} "
                          f"(expected {expected[:12]}..., got {actual[:12]}...). Download discarded.")
    log(f"  SHA-256 verified ({actual[:16]}...)")


# ------------------------------------------------------------------ extract
def _extract(archive: Path, tools: Tuple[str, ...], dest: Path) -> List[str]:
    """Extract only the wanted executables (by base name) - immune to path traversal."""
    wanted = {(t + EXE).lower(): t for t in tools}
    found: List[str] = []

    def write(stream, name: str) -> None:
        out = dest / (name + EXE)
        with open(out, "wb") as fh:
            shutil.copyfileobj(stream, fh)
        out.chmod(out.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH | stat.S_IRUSR)
        found.append(name)

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                base = info.filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
                if not info.is_dir() and base in wanted and wanted[base] not in found:
                    with z.open(info) as src:
                        write(src, wanted[base])
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as t:
            for member in t.getmembers():
                base = member.name.rsplit("/", 1)[-1].lower()
                if member.isfile() and base in wanted and wanted[base] not in found:
                    src = t.extractfile(member)
                    if src is not None:
                        write(src, wanted[base])
    else:
        raise PrereqError(f"downloaded file is not a zip/tar archive: {archive.name}")
    missing = [t for t in tools if t not in found]
    if missing:
        raise PrereqError(f"archive did not contain: {', '.join(missing)}")
    return found


def _run_ok(path: Path) -> Tuple[bool, str]:
    try:
        p = subprocess.run([str(path), "-hide_banner", "-version"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=60)
        return p.returncode == 0, (p.stdout or "").splitlines()[0] if p.stdout else ""
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


# ------------------------------------------------------------------ install
def install_ffmpeg(log: Logger = _print) -> Tuple[Path, Path]:
    """Download, verify and install private ffmpeg + ffprobe. Returns their paths."""
    from mp3cutter.ffmpeg import has_mp3_encoder

    plan = download_plan()
    target = managed_dir()
    target.mkdir(parents=True, exist_ok=True)
    log(f"Installing FFmpeg for mp3cut into {target}")
    with tempfile.TemporaryDirectory(prefix="mp3cut-ffmpeg-") as tmp_s:
        tmp = Path(tmp_s)
        staged = tmp / "bin"
        staged.mkdir()
        sources = []
        for i, arc in enumerate(plan):
            label = "+".join(arc.tools)
            log(f"  Downloading {label}: {arc.url}")
            path = tmp / f"download-{i}"
            final = download(arc.url, path, label=f"{label:<15}")
            _verify(arc, path, final, log)
            _extract(path, arc.tools, staged)
            sources.append({"tools": list(arc.tools), "url": final, "sha256": sha256(path)})
            path.unlink()

        if pi.os_name() == "macOS" and shutil.which("xattr"):
            for t in TOOLS:   # files fetched by urllib aren't quarantined, but curl/mirrors may be
                subprocess.run(["xattr", "-d", "com.apple.quarantine", str(staged / t)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        versions = {}
        for t in TOOLS:
            ok, out = _run_ok(staged / (t + EXE))
            if not ok:
                raise PrereqError(f"downloaded {t} does not run on this system: {out[:200]}")
            versions[t] = out
        if not has_mp3_encoder(str(staged / ("ffmpeg" + EXE))):
            raise PrereqError("downloaded ffmpeg has no MP3 encoder (libmp3lame)")
        for t in TOOLS:
            os.replace(staged / (t + EXE), target / (t + EXE))
    (target / "SOURCE.json").write_text(json.dumps(
        {"installed_by": f"mp3cut {__version__}", "installed": time.strftime("%Y-%m-%d %H:%M:%S"),
         "sources": sources, "versions": versions}, indent=2), encoding="utf-8")
    log(f"  FFmpeg ready: {versions['ffmpeg']}")
    return target / ("ffmpeg" + EXE), target / ("ffprobe" + EXE)


def ask(question: str, assume_yes: bool, default: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        return default
    try:
        ans = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    except EOFError:
        return default
    return default if not ans else ans in ("y", "yes")


def offer_install(assume_yes: bool = False, reason: str = "FFmpeg is required but was not found.",
                  non_interactive_default: bool = False, log: Logger = _print) -> bool:
    """Ask to download a private FFmpeg; returns True when it was installed."""
    log(reason)
    if not assume_yes and (not sys.stdin or not sys.stdin.isatty()) and not non_interactive_default:
        return False
    if not ask(f"Download FFmpeg for mp3cut now ({approx_size()}, into {managed_dir()}, no admin needed)?",
               assume_yes, default=True):
        return False
    try:
        install_ffmpeg(log)
        return True
    except PrereqError as exc:
        log(f"FFmpeg download failed: {exc}")
        return False


def remove_managed() -> bool:
    d = managed_dir()
    if not d.exists():
        return False
    shutil.rmtree(d)
    parent = d.parent
    try:
        parent.rmdir()          # only if empty
    except OSError:
        pass
    return True
