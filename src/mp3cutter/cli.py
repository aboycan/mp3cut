"""Command-line interface for mp3cut (entry point: ``mp3cut = mp3cutter.cli:main``)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from mp3cutter import __version__
from mp3cutter import core
from mp3cutter.ffmpeg import find_tool, has_mp3_encoder, install_hint

EXAMPLES = """\
examples:
  mp3cut --input song.mp3 --start 01:30 --end 02:45
  mp3cut movie.mp4                                  extract the whole audio track as MP3
  mp3cut movie.mkv --start 1:02:10 --duration 3m    cut 3 minutes of audio from a video
  mp3cut podcast.m4a --split-every 10:00            split into 10-minute MP3 parts
  mp3cut concert.flac --parts 4 --output parts/     split into 4 equal parts
  mp3cut movie.mkv --info                           list audio tracks (use with --audio-stream)
  mp3cut "https://www.youtube.com/watch?v=ID" -s 1:30 -e 2:45
                                                    cut from a YouTube link -> output/<title>_01m30s-02m45s.mp3

time formats: 90 | 90.5 | 1:30 | 01:30.250 | 1:02:03 | 1m30s | 1h2m
input: any file FFmpeg can read (mp3, wav, flac, m4a, aac, ogg, opus, wma, mp4, mkv, mov, avi, webm, ...)
       or a YouTube link (put it in quotes); link outputs go to the "output" folder of the current directory
output: always MP3, 320 kbps CBR by default

setup & troubleshooting:
  mp3cut --check-env       diagnose installation, PATH and FFmpeg
  python -m mp3cutter ...  works even if the mp3cut command is not on PATH
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mp3cut",
        description="Cut, split or extract audio from any audio/video file into high-quality MP3.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("input_pos", nargs="?", metavar="INPUT", help="input media file or YouTube link (same as --input)")

    io = p.add_argument_group("input / output")
    io.add_argument("-i", "--input", metavar="FILE|URL", help="input audio/video file or YouTube link")
    io.add_argument("-o", "--output", metavar="PATH",
                    help="output MP3 file, or a folder (folder is required when splitting); "
                         "default: next to the input")
    io.add_argument("--overwrite", action="store_true", help="replace existing output files instead of "
                                                             "writing 'name (1).mp3'")

    rng = p.add_argument_group("range")
    rng.add_argument("-s", "--start", metavar="TIME", help="start time (default: beginning)")
    end = rng.add_mutually_exclusive_group()
    end.add_argument("-e", "--end", metavar="TIME", help="end time (default: end of file)")
    end.add_argument("-d", "--duration", metavar="TIME", help="length to keep, instead of --end")

    sp = p.add_argument_group("splitting")
    spx = sp.add_mutually_exclusive_group()
    spx.add_argument("--split-every", metavar="TIME", help="split the range into parts of this length")
    spx.add_argument("--parts", type=int, metavar="N", help="split the range into N equal parts")

    au = p.add_argument_group("audio")
    au.add_argument("-b", "--bitrate", default=core.DEFAULT_BITRATE, metavar="RATE",
                    help="MP3 bitrate (default: 320k)")
    au.add_argument("-a", "--audio-stream", type=int, metavar="N",
                    help="which audio track to use, 0-based (default: the file's default track)")

    misc = p.add_argument_group("information & setup")
    misc.add_argument("--info", action="store_true", help="show the file's duration and audio tracks, then exit")
    misc.add_argument("--check-env", action="store_true", help="diagnose installation, PATH and FFmpeg")
    misc.add_argument("--install-ffmpeg", action="store_true",
                      help="download/update mp3cut's private FFmpeg + FFprobe (no admin needed)")
    misc.add_argument("--update-ytdlp", action="store_true",
                      help="install/update yt-dlp (and refresh mp3cut's private Deno) for YouTube links")
    misc.add_argument("--setup-path", action="store_true", help="(re)configure PATH so 'mp3cut' works everywhere")
    misc.add_argument("--remove-path", action="store_true", help="undo PATH changes made by the installer")
    misc.add_argument("--yes", action="store_true", help="answer yes to setup prompts")
    misc.add_argument("-q", "--quiet", action="store_true", help="only print the created file paths and errors")
    misc.add_argument("-V", "--version", action="version", version=f"mp3cut {__version__}")
    return p


def _err(msg: str) -> None:
    print(f"mp3cut: error: {msg}", file=sys.stderr)


def _update_ytdlp(assume_yes: bool) -> int:
    from mp3cutter import youtube
    from mp3cutter.prereqs import PrereqError, find_deno, install_deno, managed_deno
    ok = youtube.pip_install_ytdlp(upgrade=True)
    try:
        if managed_deno() is not None or find_deno() is None:
            install_deno()
    except PrereqError as exc:
        _err(f"Deno: {exc}")
        ok = False
    return 0 if ok else 2


def _ensure_ffmpeg(assume_yes: bool):
    """Find ffmpeg/ffprobe; if missing, offer to download mp3cut's private copy (interactive only)."""
    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe
    from mp3cutter.prereqs import offer_install
    if offer_install(assume_yes):
        return find_tool("ffmpeg"), find_tool("ffprobe")
    return ffmpeg, ffprobe


def _normalize_single_dash(parser: argparse.ArgumentParser, argv: List[str]) -> List[str]:
    """Accept FFmpeg-style single-dash long options: -input -> --input, -start=1:30 -> --start=1:30."""
    long_names = {opt[2:] for action in parser._actions for opt in action.option_strings if opt.startswith("--")}
    out: List[str] = []
    for i, tok in enumerate(argv):
        if tok == "--":
            return out + argv[i:]
        if tok.startswith("-") and not tok.startswith("--") and len(tok) > 2:
            name = tok[1:].split("=", 1)[0]
            if name in long_names:
                tok = "-" + tok
        out.append(tok)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_single_dash(parser, sys.argv[1:] if argv is None else argv))

    if args.check_env:
        from mp3cutter.envcheck import run_check
        return run_check()
    if args.install_ffmpeg:
        from mp3cutter.prereqs import PrereqError, install_ffmpeg
        try:
            install_ffmpeg()
            return 0
        except PrereqError as exc:
            _err(str(exc))
            return 2
    if args.update_ytdlp:
        return _update_ytdlp(args.yes)
    if args.setup_path:
        from mp3cutter.pathsetup import install_flow
        return install_flow(assume_yes=args.yes)
    if args.remove_path:
        from mp3cutter.pathsetup import remove_flow
        return remove_flow(assume_yes=args.yes)

    if args.input and args.input_pos and args.input != args.input_pos:
        _err("give the input file either as --input or as a positional argument, not both")
        return 1
    src = args.input or args.input_pos
    if not src:
        parser.print_help()
        return 1

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, file=sys.stderr)

    ffmpeg, ffprobe = _ensure_ffmpeg(args.yes)
    if not ffmpeg or not ffprobe:
        missing = " and ".join(n for n, v in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not v)
        _err(f"{missing} not found.\n{install_hint()}")
        return 2

    try:
        bitrate = core.normalize_bitrate(args.bitrate)
        start = core.parse_time(args.start) if args.start else None
        end = core.parse_time(args.end) if args.end else None
        duration = core.parse_time(args.duration) if args.duration else None
        times = (start, end, duration)

        if not args.info and not has_mp3_encoder(ffmpeg):
            from mp3cutter.prereqs import offer_install
            if offer_install(args.yes, reason=f"The FFmpeg at {ffmpeg} has no MP3 encoder (libmp3lame)."):
                ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
            else:
                raise core.Mp3CutError("this FFmpeg build has no MP3 encoder (libmp3lame).\n" + install_hint(), 2)

        from mp3cutter import youtube
        if youtube.is_url(src):
            return _run_link(args, youtube.normalize_url(src), ffmpeg, ffprobe, bitrate, times, log)

        src_path = Path(src).expanduser()
        info = core.probe(src_path, ffprobe)
        if args.info:
            print(core.describe(info))
            return 0
        _process(args, src_path, info, ffmpeg, bitrate, times, log)
        return 0
    except core.Mp3CutError as exc:
        _err(str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        print("\nmp3cut: interrupted", file=sys.stderr)
        return 130


def _process(args, src_path: Path, info: "core.MediaInfo", ffmpeg: str, bitrate: str, times, log,
             name: Optional[str] = None, default_dir: Optional[Path] = None, label: Optional[str] = None) -> None:
    """Cut or split an already-probed local media file."""
    start, end, duration = times
    stream = core.select_stream(info, args.audio_stream)
    kind = "video + audio" if info.has_video else "audio"
    log(f"Input  : {label or src_path.name} ({kind}, {core.format_time(info.duration)})")
    log(f"Audio  : {stream.describe()}"
        + (f"  [{len(info.audio_streams)} tracks, choose with --audio-stream]" if len(info.audio_streams) > 1 else ""))
    log(f"Output : MP3 {bitrate[:-1]} kbps CBR, {core.target_sample_rate(stream.sample_rate)} Hz"
        + (", downmixed to stereo" if (stream.channels or 0) > 2 else ""))

    job = core.Job(src=src_path, ffmpeg=ffmpeg, info=info, stream=stream, bitrate=bitrate,
                   overwrite=args.overwrite, show_progress=not args.quiet and sys.stderr.isatty(), log=log,
                   name=name, default_dir=default_dir)

    if args.split_every or args.parts:
        every = core.parse_time(args.split_every) if args.split_every else None
        files = core.split(job, every=every, parts=args.parts, start=start, end=end,
                           duration=duration, output=args.output)
        for f in files:
            print(f)
        log(f"Done: {len(files)} file(s) in {files[0].parent if files else '-'}")
    else:
        s, e = core.resolve_range(info, start, end, duration)
        stop = e if e is not None else info.duration
        log(f"Range  : {core.format_time(s)} -> {core.format_time(stop)}"
            + (f"  ({core.format_time(stop - s)})" if stop is not None else ""))
        print(core.cut(job, start=start, end=end, duration=duration, output=args.output))


def _run_link(args, url: str, ffmpeg: str, ffprobe: str, bitrate: str, times, log) -> int:
    """YouTube (or other yt-dlp supported) link: download audio only, then cut into ./output/."""
    import tempfile
    from mp3cutter import youtube

    youtube.ensure_ytdlp(args.yes, log)
    js = youtube.js_runtime_options(args.yes, log)
    log(f"Link   : {url}")
    info = youtube.fetch_info(url, js)
    title = info.get("title") or info.get("id") or "audio"
    total = info.get("duration")
    total = float(total) if isinstance(total, (int, float)) and total > 0 else None
    who = info.get("uploader") or info.get("channel")
    if args.info:
        print(f"Title    : {title}")
        if who:
            print(f"Channel  : {who}")
        print(f"Duration : {core.format_time(total)}")
        print(f"Output   : {Path.cwd() / 'output' / (youtube.safe_filename(title, info.get('id') or 'audio') + '_<start>-<end>.mp3')}")
        return 0
    log(f"Title  : {title}" + (f"  [{who}]" if who else "") + f"  ({core.format_time(total)})")

    start, end, duration = times
    if total is not None:   # check the range before downloading anything
        core.resolve_range(core.MediaInfo(path=Path(title), format_name="", duration=total), start, end, duration)

    name = youtube.safe_filename(title, info.get("id") or "audio")
    out_dir = Path.cwd() / "output"
    with tempfile.TemporaryDirectory(prefix="mp3cut-link-") as tmp:
        audio = youtube.download_audio(url, Path(tmp), ffmpeg, js,
                                       show_progress=not args.quiet and sys.stderr.isatty(), log=log)
        media = core.probe(audio, ffprobe)
        _process(args, audio, media, ffmpeg, bitrate, times, log, name=name, default_dir=out_dir,
                 label=f"{audio.suffix.lstrip('.')} audio from link")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
