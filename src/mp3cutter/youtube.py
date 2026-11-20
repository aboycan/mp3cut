"""YouTube (and other site) links as mp3cut input, via yt-dlp.

Flow: fetch the video's info -> download the best audio-only stream to a temp
folder -> hand the file to the normal cut/split pipeline -> MP3 in ./output/.

YouTube needs two things besides yt-dlp itself (see the yt-dlp EJS wiki):
  * yt-dlp-ejs   - installed with ``pip install "yt-dlp[default]"``
  * a JavaScript runtime - Deno; mp3cut downloads a private copy if needed
"""

from __future__ import annotations

import importlib
import os
import re
import shutil
import site
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, Optional

from mp3cutter import platform_info as pi
from mp3cutter.core import Mp3CutError

YTDLP_SPEC = "yt-dlp[default]"
Logger = Callable[[str], None]

_URL_RE = re.compile(r"^(https?://|www\.|(m\.|music\.)?youtube\.com/|youtu\.be/)", re.I)
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _print(msg: str) -> None:
    print(msg, file=sys.stderr)


# --------------------------------------------------------------- inputs/names
def is_url(text: str) -> bool:
    return bool(_URL_RE.match(text.strip()))


def normalize_url(text: str) -> str:
    text = text.strip()
    return text if re.match(r"^https?://", text, re.I) else "https://" + text


def safe_filename(title: Optional[str], fallback: str = "audio", max_len: int = 150) -> str:
    """Make a video title safe as a file name on Windows, macOS and Linux."""
    name = _BAD_CHARS.sub("_", title or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = _BAD_CHARS.sub("_", fallback).strip(" .") or "audio"
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name


# ------------------------------------------------------------------- yt-dlp
def ytdlp_version() -> Optional[str]:
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        return None


def ejs_version() -> Optional[str]:
    try:
        import yt_dlp_ejs
        return getattr(yt_dlp_ejs, "version", None) or getattr(yt_dlp_ejs, "__version__", None) or "installed"
    except Exception:
        return None


def _refresh_import_path() -> None:
    # a first-time --user install creates the user site folder after Python started
    try:
        user_site = site.getusersitepackages()
        if user_site and os.path.isdir(user_site) and user_site not in sys.path:
            site.addsitedir(user_site)
    except Exception:
        pass
    importlib.invalidate_caches()


def pip_install_ytdlp(upgrade: bool = False, log: Logger = _print) -> bool:
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location"]
    if upgrade:
        cmd.append("--upgrade")
    if not pi.in_venv():
        cmd.append("--user")
    log(f"{'Updating' if upgrade else 'Installing'} yt-dlp: {' '.join(cmd[2:])} \"{YTDLP_SPEC}\"")
    proc = subprocess.run(cmd + [YTDLP_SPEC], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 and "externally-managed-environment" in (proc.stdout or ""):
        proc = subprocess.run(cmd + ["--break-system-packages", YTDLP_SPEC], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        log("\n".join((proc.stdout or "").strip().splitlines()[-6:]))
        return False
    _refresh_import_path()
    for mod in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
        del sys.modules[mod]           # pick up the new version on upgrade
    log(f"  yt-dlp {ytdlp_version() or '?'} ready")
    return ytdlp_version() is not None


def ensure_ytdlp(assume_yes: bool, log: Logger = _print) -> None:
    if ytdlp_version():
        return
    _refresh_import_path()
    if ytdlp_version():
        return
    from mp3cutter.prereqs import ask
    log("YouTube links need yt-dlp, which is not installed for this Python.")
    interactive = bool(sys.stdin and sys.stdin.isatty())
    if (assume_yes or interactive) and ask("Install yt-dlp now (pip, user-level)?", assume_yes, default=True):
        if pip_install_ytdlp(log=log):
            return
    raise Mp3CutError("yt-dlp is required for links. Install it with:  mp3cut --update-ytdlp", 2)


# --------------------------------------------------------------- JS runtime
def _parsed_js_runtimes(arg: str) -> Dict:
    """Let yt-dlp translate --js-runtimes into its own option format."""
    try:
        import yt_dlp
        parsed = yt_dlp.parse_options(["--js-runtimes", arg])
        opts = getattr(parsed, "ydl_opts", None) or parsed[-1]
        return {"js_runtimes": opts["js_runtimes"]} if opts.get("js_runtimes") else {}
    except Exception:
        return {}


def js_runtime_options(assume_yes: bool, log: Logger = _print) -> Dict:
    """Make Deno (preferred) or Node available to yt-dlp; offer to download Deno if neither exists."""
    from mp3cutter.prereqs import find_deno, offer_deno
    deno = find_deno()
    if deno is None and offer_deno(assume_yes, log=log):
        deno = find_deno()
    if deno:
        folder = str(Path(deno).parent)
        if not pi.dir_in_path_value(Path(folder)):
            os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")   # yt-dlp finds deno on PATH
        return _parsed_js_runtimes(f"deno:{deno}")
    node = shutil.which("node")
    if node:
        log("Note: using Node.js as the JavaScript runtime for YouTube.")
        return _parsed_js_runtimes(f"node:{node}")
    log("Warning: no JavaScript runtime; YouTube downloads may fail. Fix: mp3cut --update-ytdlp")
    return {}


# ----------------------------------------------------------------- download
def _clean_error(exc: BaseException) -> str:
    msg = str(exc).strip()
    msg = re.sub(r"^(ERROR:\s*)+", "", msg)
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)
    return msg.splitlines()[0] if msg else exc.__class__.__name__


def _base_opts(extra: Optional[Dict] = None) -> Dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True, "retries": 5,
            "socket_timeout": 30}
    if extra:
        opts.update(extra)
    return opts


def fetch_info(url: str, extra_opts: Optional[Dict] = None) -> Dict:
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL(_base_opts(extra_opts)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise Mp3CutError(f"could not read link: {_clean_error(exc)}") from exc
    if not info:
        raise Mp3CutError("could not read link: no information returned")
    if info.get("_type") == "playlist" or info.get("entries") is not None:
        raise Mp3CutError("that is a playlist link; give the link of a single video")
    return info


def download_audio(url: str, workdir: Path, ffmpeg: str, extra_opts: Optional[Dict] = None,
                   show_progress: bool = True, log: Logger = _print) -> Path:
    """Download the best audio-only stream into workdir; returns the downloaded file."""
    import yt_dlp

    def hook(d: Dict) -> None:
        if not show_progress:
            return
        if d.get("status") == "downloading":
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            frac = min(done / total, 1.0) if total else 0.0
            bar = "#" * int(frac * 30)
            sys.stderr.write(f"\r  Downloading [{bar:<30}] {frac * 100:5.1f}%  {done / 1_048_576:6.1f} MB")
            sys.stderr.flush()
        elif d.get("status") == "finished":
            sys.stderr.write("\n")
            sys.stderr.flush()

    opts = _base_opts(extra_opts)
    opts.update({
        "format": "bestaudio/best",
        "outtmpl": {"default": str(workdir / "%(id)s.%(ext)s")},
        "ffmpeg_location": str(Path(ffmpeg).parent),
        "progress_hooks": [hook],
        "overwrites": True,
    })
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            candidates = [Path(d["filepath"]) for d in (info or {}).get("requested_downloads") or []
                          if d.get("filepath")]
            if not candidates and info:
                candidates.append(Path(ydl.prepare_filename(info)))
    except Exception as exc:
        raise Mp3CutError(f"download failed: {_clean_error(exc)}") from exc
    for c in candidates:
        if c.is_file():
            return c
    found = sorted((p for p in workdir.iterdir() if p.is_file() and not p.name.endswith(".part")),
                   key=lambda p: p.stat().st_size, reverse=True)
    if found:
        return found[0]
    raise Mp3CutError("download finished but no audio file was produced")
