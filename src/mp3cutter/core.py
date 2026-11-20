"""Audio processing: probe any media file, cut / split it, always output MP3.

This module is independent of how mp3cut is installed; it only needs the
paths of ``ffmpeg`` and ``ffprobe``.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

DEFAULT_BITRATE = "320k"
MP3_SAMPLE_RATES = (32000, 44100, 48000)   # MPEG-1 Layer III rates (needed for 320 kbps)
MAX_SPLIT_FILES = 10000

Logger = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


class Mp3CutError(Exception):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


# ----------------------------------------------------------------------- times
_UNIT_RE = re.compile(
    r"^(?:(?P<h>\d+(?:\.\d+)?)h)?(?:(?P<m>\d+(?:\.\d+)?)m)?(?:(?P<s>\d+(?:\.\d+)?)s?)?$"
)


def parse_time(value: str) -> float:
    """Parse ``90``, ``90.5``, ``1:30``, ``01:30.250``, ``1:02:03``, ``1m30s``, ``1h2m`` into seconds."""
    text = str(value).strip().lower()
    bad = Mp3CutError(f"invalid time '{value}' (use SS, MM:SS, HH:MM:SS[.mmm] or 1h2m3s)")
    if not text:
        raise bad
    if ":" in text:
        parts = text.split(":")
        if len(parts) > 3 or any(p == "" for p in parts):
            raise bad
        if any(not p.isdigit() for p in parts[:-1]):
            raise bad
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            raise bad from None
        if nums[-1] >= 60 or (len(nums) == 3 and nums[1] >= 60):
            raise Mp3CutError(f"invalid time '{value}': minutes and seconds must be below 60")
        total = 0.0
        for n in nums:
            total = total * 60 + n
        return total
    m = _UNIT_RE.match(text)
    if m and any(m.group(g) for g in ("h", "m", "s")):
        return (float(m.group("h") or 0) * 3600 + float(m.group("m") or 0) * 60
                + float(m.group("s") or 0))
    raise bad


def _split_ms(seconds: float) -> Tuple[int, int, int, int]:
    ms = int(round(max(seconds, 0.0) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return h, m, s, ms


def format_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    h, m, s, ms = _split_ms(seconds)
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    return base + (f".{ms:03d}" if ms else "")


def time_label(seconds: float) -> str:
    """Filename-safe label, e.g. 01m30s, 1h02m03s, 00m05.5s."""
    h, m, s, ms = _split_ms(seconds)
    frac = f".{ms:03d}".rstrip("0") if ms else ""
    return (f"{h}h" if h else "") + f"{m:02d}m{s:02d}{frac}s"


def normalize_bitrate(value: str) -> str:
    m = re.fullmatch(r"\s*(\d+)\s*(k|kbps|kb/s)?\s*", str(value).lower())
    if not m:
        raise Mp3CutError(f"invalid bitrate '{value}' (example: 320k)")
    kbps = int(m.group(1))
    allowed = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    if kbps not in allowed:
        raise Mp3CutError(f"invalid MP3 bitrate {kbps}k; allowed: {', '.join(str(a) for a in allowed)}")
    return f"{kbps}k"


# ----------------------------------------------------------------------- probe
@dataclass
class AudioStream:
    index: int            # absolute stream index inside the container
    audio_index: int      # 0-based position among audio streams (for --audio-stream)
    codec: str
    sample_rate: Optional[int]
    channels: Optional[int]
    language: Optional[str] = None
    title: Optional[str] = None
    default: bool = False

    def describe(self) -> str:
        bits = [self.codec]
        if self.sample_rate:
            bits.append(f"{self.sample_rate} Hz")
        if self.channels:
            bits.append({1: "mono", 2: "stereo"}.get(self.channels, f"{self.channels} ch"))
        text = f"#{self.audio_index}: " + ", ".join(bits)
        if self.language and self.language != "und":
            text += f" [{self.language}]"
        if self.title:
            text += f" \"{self.title}\""
        if self.default:
            text += " (default)"
        return text


@dataclass
class MediaInfo:
    path: Path
    format_name: str
    duration: Optional[float]
    audio_streams: List[AudioStream] = field(default_factory=list)
    video_codecs: List[str] = field(default_factory=list)

    @property
    def has_video(self) -> bool:
        return bool(self.video_codecs)


def _to_float(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _to_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _arg_path(path: Path) -> str:
    # absolute paths stop ffmpeg treating "-name" as an option or "x:" as a protocol
    return os.path.abspath(str(path))


def probe(path: Path, ffprobe: str) -> MediaInfo:
    path = Path(path).expanduser()
    if not path.exists():
        raise Mp3CutError(f"input file not found: {path}")
    if not path.is_file():
        raise Mp3CutError(f"input is not a file: {path}")
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", _arg_path(path)]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise Mp3CutError(f"could not run ffprobe: {exc}", 2) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()[-1:] or ["unrecognised format"]
        raise Mp3CutError(f"cannot read '{path.name}' as media: {detail[0]}")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise Mp3CutError(f"unexpected ffprobe output for '{path.name}'") from exc

    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    duration = _to_float(fmt.get("duration"))
    audio: List[AudioStream] = []
    video: List[str] = []
    for st in streams:
        kind = st.get("codec_type")
        disp = st.get("disposition", {}) or {}
        tags = st.get("tags", {}) or {}
        if kind == "audio":
            audio.append(AudioStream(
                index=int(st.get("index", len(audio))),
                audio_index=len(audio),
                codec=st.get("codec_name", "unknown"),
                sample_rate=_to_int(st.get("sample_rate")),
                channels=_to_int(st.get("channels")),
                language=tags.get("language"),
                title=tags.get("title"),
                default=bool(disp.get("default")),
            ))
            if duration is None:
                duration = _to_float(st.get("duration"))
        elif kind == "video" and not disp.get("attached_pic"):
            video.append(st.get("codec_name", "video"))
    return MediaInfo(path=path, format_name=fmt.get("format_long_name") or fmt.get("format_name", "unknown"),
                     duration=duration, audio_streams=audio, video_codecs=video)


def describe(info: MediaInfo) -> str:
    lines = [
        f"File     : {info.path}",
        f"Format   : {info.format_name}",
        f"Duration : {format_time(info.duration)}",
        f"Video    : {', '.join(info.video_codecs) if info.has_video else 'none'}",
        "Audio    : " + ("none" if not info.audio_streams else ""),
    ]
    for st in info.audio_streams:
        lines.append(f"  {st.describe()}")
    return "\n".join(lines)


def select_stream(info: MediaInfo, requested: Optional[int]) -> AudioStream:
    streams = info.audio_streams
    if not streams:
        raise Mp3CutError(f"'{info.path.name}' contains no audio stream")
    if requested is not None:
        if not 0 <= requested < len(streams):
            raise Mp3CutError(f"--audio-stream {requested} does not exist; available: 0..{len(streams) - 1} "
                              "(see: mp3cut --info FILE)")
        return streams[requested]
    return next((s for s in streams if s.default), streams[0])


# ----------------------------------------------------------------- encoding
def target_sample_rate(src_rate: Optional[int]) -> int:
    if src_rate in MP3_SAMPLE_RATES:
        return src_rate  # type: ignore[return-value]
    if src_rate and src_rate % 44100 == 0:
        return 44100
    if src_rate and src_rate > 48000:
        return 48000
    return 44100


def build_command(ffmpeg: str, src: Path, dst: Path, *, start: float, duration: Optional[float],
                  stream: AudioStream, bitrate: str) -> List[str]:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]      # input seek: fast, and frame-accurate when re-encoding
    cmd += ["-i", _arg_path(src)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [
        "-map", f"0:{stream.index}",
        "-vn", "-sn", "-dn",
        "-map_metadata", "0", "-map_chapters", "-1",
        "-c:a", "libmp3lame", "-b:a", bitrate,
    ]
    rate = target_sample_rate(stream.sample_rate)
    if rate != stream.sample_rate:
        cmd += ["-ar", str(rate)]
    if stream.channels and stream.channels > 2:
        cmd += ["-ac", "2"]                 # MP3 is mono/stereo only: downmix 5.1 etc.
    cmd += ["-id3v2_version", "3", "-f", "mp3", "-progress", "pipe:1", "-nostats", _arg_path(dst)]
    return cmd


def _draw_progress(done: float, total: float, label: str) -> None:
    frac = min(max(done / total, 0.0), 1.0) if total > 0 else 0.0
    width = 30
    filled = int(frac * width)
    sys.stderr.write(f"\r  {label} [{'#' * filled}{'.' * (width - filled)}] {frac * 100:5.1f}%")
    sys.stderr.flush()


def run_ffmpeg(cmd: List[str], expected_seconds: Optional[float], show_progress: bool, label: str) -> None:
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
    except OSError as exc:
        raise Mp3CutError(f"could not run ffmpeg: {exc}", 2) from exc
    err_lines: List[str] = []
    reader = threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True)  # type: ignore[arg-type]
    reader.start()
    drew = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            key, _, val = line.strip().partition("=")
            if show_progress and expected_seconds and key in ("out_time_us", "out_time_ms"):
                try:
                    secs = int(val) / 1_000_000   # both keys are microseconds in ffmpeg
                except ValueError:
                    continue
                _draw_progress(secs, expected_seconds, label)
                drew = True
            elif show_progress and expected_seconds and key == "progress" and val == "end":
                _draw_progress(expected_seconds, expected_seconds, label)
                drew = True
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise
    finally:
        if drew:
            sys.stderr.write("\n")
            sys.stderr.flush()
    reader.join(timeout=2)
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()
    if proc.returncode != 0:
        tail = "".join(err_lines).strip().splitlines()[-3:]
        raise Mp3CutError("ffmpeg failed: " + (" | ".join(tail) or f"exit code {proc.returncode}"), 3)


def _encode(ffmpeg: str, src: Path, dst: Path, start: float, end: Optional[float], total: Optional[float],
            stream: AudioStream, bitrate: str, show_progress: bool, label: str) -> None:
    # write to a temp file next to the target, then rename: never leaves a half-written MP3
    tmp = dst.with_name(f".{dst.stem}.mp3cut-tmp-{os.getpid()}.mp3")
    duration = (end - start) if end is not None else None
    expected = duration if duration is not None else ((total - start) if total else None)
    cmd = build_command(ffmpeg, src, tmp, start=start, duration=duration, stream=stream, bitrate=bitrate)
    try:
        run_ffmpeg(cmd, expected, show_progress, label)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ------------------------------------------------------------------- outputs
def resolve_range(info: MediaInfo, start: Optional[float], end: Optional[float], duration: Optional[float],
                  log: Logger = _noop) -> Tuple[float, Optional[float]]:
    start = start or 0.0
    if start < 0:
        raise Mp3CutError("start time cannot be negative")
    if duration is not None:
        if duration <= 0:
            raise Mp3CutError("duration must be greater than zero")
        end = start + duration
    total = info.duration
    if total is not None:
        if start >= total:
            raise Mp3CutError(f"start {format_time(start)} is at or beyond the end of the file ({format_time(total)})")
        if end is not None and end > total + 0.001:
            log(f"Note: end {format_time(end)} is past the end of the file; using {format_time(total)}")
            end = total
    if end is not None and end <= start:
        raise Mp3CutError(f"end {format_time(end)} must be after start {format_time(start)}")
    return start, end


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def _same_file(a: Path, b: Path) -> bool:
    try:
        return b.exists() and os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def resolve_output(src: Path, output: Optional[str], default_name: str, overwrite: bool,
                   log: Logger = _noop, default_dir: Optional[Path] = None) -> Path:
    if output:
        out = Path(output).expanduser()
        if out.is_dir() or output.endswith(("/", "\\", os.sep)):
            out.mkdir(parents=True, exist_ok=True)
            out = out / default_name
        else:
            if out.suffix.lower() != ".mp3":
                fixed = out.with_suffix(".mp3") if out.suffix else out.with_name(out.name + ".mp3")
                log(f"Note: output is always MP3; writing {fixed.name}")
                out = fixed
            out.parent.mkdir(parents=True, exist_ok=True)
    else:
        base = default_dir or src.parent
        base.mkdir(parents=True, exist_ok=True)
        out = base / default_name
    if _same_file(src, out) or os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(out)):
        raise Mp3CutError("output would overwrite the input file; choose a different --output")
    if out.exists() and not overwrite:
        new = _unique(out)
        log(f"Note: {out.name} exists; writing {new.name} (use --overwrite to replace)")
        out = new
    return out


def default_cut_name(stem: str, is_mp3: bool, start: float, end: Optional[float], total: Optional[float],
                     bitrate: str) -> str:
    if start == 0 and end is None:
        return f"{stem}.mp3" if not is_mp3 else f"{stem}_{bitrate}.mp3"
    stop = end if end is not None else total
    stop_label = time_label(stop) if stop is not None else "end"
    return f"{stem}_{time_label(start)}-{stop_label}.mp3"


@dataclass
class Job:
    src: Path
    ffmpeg: str
    info: MediaInfo
    stream: AudioStream
    bitrate: str = DEFAULT_BITRATE
    overwrite: bool = False
    show_progress: bool = True
    log: Logger = _noop
    name: Optional[str] = None          # output name stem (default: input file name); e.g. a video title
    default_dir: Optional[Path] = None  # where outputs go without --output (default: next to the input)

    @property
    def stem(self) -> str:
        return self.name or self.src.stem

    @property
    def base_dir(self) -> Path:
        return self.default_dir or self.src.parent


def cut(job: Job, start: Optional[float] = None, end: Optional[float] = None,
        duration: Optional[float] = None, output: Optional[str] = None) -> Path:
    start, end = resolve_range(job.info, start, end, duration, job.log)
    is_mp3 = job.name is None and job.src.suffix.lower() == ".mp3"
    name = default_cut_name(job.stem, is_mp3, start, end, job.info.duration, job.bitrate)
    dst = resolve_output(job.src, output, name, job.overwrite, job.log, job.default_dir)
    _encode(job.ffmpeg, job.src, dst, start, end, job.info.duration, job.stream, job.bitrate,
            job.show_progress, "Encoding")
    return dst


def split(job: Job, every: Optional[float] = None, parts: Optional[int] = None,
          start: Optional[float] = None, end: Optional[float] = None, duration: Optional[float] = None,
          output: Optional[str] = None) -> List[Path]:
    start, end = resolve_range(job.info, start, end, duration, job.log)
    stop = end if end is not None else job.info.duration
    if stop is None:
        raise Mp3CutError("cannot split: the file's duration is unknown (give --end)")
    span = stop - start
    if parts is not None:
        if parts < 1:
            raise Mp3CutError("--parts must be at least 1")
        seg = span / parts
        count = parts
    else:
        if not every or every <= 0:
            raise Mp3CutError("--split-every must be greater than zero")
        seg = every
        count = max(1, math.ceil(span / seg - 1e-6))
        # fold a tiny trailing remainder (e.g. 0.04 s) into the previous part instead of a broken stub file
        if count > 1 and span - (count - 1) * seg < min(1.0, seg / 2):
            count -= 1
    if count > MAX_SPLIT_FILES:
        raise Mp3CutError(f"that would create {count} files (limit {MAX_SPLIT_FILES}); use a longer segment")

    out_dir = Path(output).expanduser() if output else job.base_dir / f"{job.stem}_parts"
    if out_dir.exists() and not out_dir.is_dir():
        raise Mp3CutError(f"--output for splitting must be a folder: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    width = max(3, len(str(count)))
    results: List[Path] = []
    for i in range(count):
        s = start + i * seg
        e = stop if i == count - 1 else min(start + (i + 1) * seg, stop)
        if e - s <= 0.01:
            continue
        name = f"{job.stem}_part{i + 1:0{width}d}_{time_label(s)}-{time_label(e)}.mp3"
        dst = out_dir / name
        if dst.exists() and not job.overwrite:
            new = _unique(dst)
            job.log(f"Note: {dst.name} exists; writing {new.name}")
            dst = new
        _encode(job.ffmpeg, job.src, dst, s, e, job.info.duration, job.stream, job.bitrate,
                job.show_progress, f"Part {i + 1}/{count}")
        results.append(dst)
    return results
