"""Run with:  python -m unittest discover -s tests   (FFmpeg tests are skipped if FFmpeg is missing)."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mp3cutter import core, pathsetup  # noqa: E402
from mp3cutter.cli import main  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class TimeParsing(unittest.TestCase):
    def test_formats(self):
        cases = {"90": 90, "90.5": 90.5, "1:30": 90, "01:30": 90, "01:30.250": 90.25,
                 "1:02:03": 3723, "1m30s": 90, "1h2m": 3720, "45s": 45, "0": 0}
        for text, want in cases.items():
            self.assertAlmostEqual(core.parse_time(text), want, msg=text)

    def test_invalid(self):
        for bad in ("", "abc", "1:2:3:4", "1:75", "-5", "1::2", "1.5:30"):
            with self.assertRaises(core.Mp3CutError, msg=bad):
                core.parse_time(bad)

    def test_labels(self):
        self.assertEqual(core.time_label(90), "01m30s")
        self.assertEqual(core.time_label(3723.5), "1h02m03.5s")
        self.assertEqual(core.format_time(165), "02:45")

    def test_bitrate(self):
        self.assertEqual(core.normalize_bitrate("320"), "320k")
        with self.assertRaises(core.Mp3CutError):
            core.normalize_bitrate("999k")

    def test_sample_rates(self):
        self.assertEqual(core.target_sample_rate(48000), 48000)
        self.assertEqual(core.target_sample_rate(96000), 48000)
        self.assertEqual(core.target_sample_rate(88200), 44100)
        self.assertEqual(core.target_sample_rate(22050), 44100)


class ShellBlock(unittest.TestCase):
    def test_idempotent_and_removable(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = Path(tmp) / ".zshrc"
            rc.write_text("export FOO=1\nalias ll='ls -l'\n")
            d = Path(tmp) / "bin dir"
            block = pathsetup.posix_block(d)
            self.assertTrue(pathsetup.upsert_block(rc, block))
            self.assertFalse(pathsetup.upsert_block(rc, block))       # second run: no change
            text = rc.read_text()
            self.assertEqual(text.count(pathsetup.BLOCK_BEGIN), 1)
            self.assertIn("export FOO=1", text)
            # the block must not duplicate PATH entries when sourced twice
            out = subprocess.run(["sh", "-c", f'PATH=/usr/bin:/bin; . "{rc}"; . "{rc}"; echo "$PATH"'],
                                 capture_output=True, text=True).stdout.strip()
            self.assertEqual(out.split(":").count(str(d)), 1)
            self.assertTrue(pathsetup.remove_block(rc))
            self.assertEqual(rc.read_text().strip(), "export FOO=1\nalias ll='ls -l'")


@unittest.skipUnless(FFMPEG and FFPROBE and os.name != "nt", "ffmpeg/ffprobe not installed")
class PrivateFFmpeg(unittest.TestCase):
    """Download/verify/extract logic, using local file:// archives instead of the internet."""

    def setUp(self):
        import zipfile
        from mp3cutter import prereqs
        self.prereqs = prereqs
        self.tmp = Path(tempfile.mkdtemp())
        self.old_env = dict(os.environ)
        os.environ["XDG_DATA_HOME"] = str(self.tmp / "data")
        os.environ["HOME"] = str(self.tmp / "home")
        self.zips = {}
        for tool, real in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)):
            z = self.tmp / f"{tool}.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.write(real, f"nested/dir/{tool}")
                zf.writestr("../../evil", "path traversal must be ignored")
            (self.tmp / f"{tool}.zip.sha256").write_text(prereqs.sha256(z) + f"  {tool}.zip\n")
            self.zips[tool] = z

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plan(self):
        return [self.prereqs.Archive(self.zips[t].as_uri(), (t,), "sidecar") for t in ("ffmpeg", "ffprobe")]

    def test_install_and_found_first(self):
        from mp3cutter.ffmpeg import find_tool, tool_origin
        self.prereqs.download_plan = self._plan
        try:
            ff, fp = self.prereqs.install_ffmpeg(log=lambda m: None)
        finally:
            import importlib
            importlib.reload(self.prereqs)
        self.assertTrue(ff.is_file() and os.access(ff, os.X_OK))
        self.assertEqual(Path(find_tool("ffmpeg")), ff)
        self.assertEqual(tool_origin(find_tool("ffprobe")), "mp3cut private copy")
        self.assertFalse((self.tmp / "evil").exists())
        self.assertEqual(sorted(p.name for p in ff.parent.iterdir()), ["SOURCE.json", "ffmpeg", "ffprobe"])
        self.assertTrue(self.prereqs.remove_managed())
        self.assertIsNone(self.prereqs.managed_tool("ffmpeg"))

    def test_checksum_mismatch_rejected(self):
        (self.tmp / "ffmpeg.zip.sha256").write_text("0" * 64 + "  ffmpeg.zip\n")
        self.prereqs.download_plan = self._plan
        try:
            with self.assertRaises(self.prereqs.PrereqError):
                self.prereqs.install_ffmpeg(log=lambda m: None)
        finally:
            import importlib
            importlib.reload(self.prereqs)
        self.assertIsNone(self.prereqs.managed_tool("ffmpeg"))

    def test_env_override_single_archive(self):
        import zipfile
        both = self.tmp / "both.zip"
        with zipfile.ZipFile(both, "w") as zf:
            zf.write(FFMPEG, "bin/ffmpeg")
            zf.write(FFPROBE, "bin/ffprobe")
        os.environ["MP3CUT_FFMPEG_URL"] = both.as_uri()
        self.prereqs.install_ffmpeg(log=lambda m: None)
        self.assertIsNotNone(self.prereqs.managed_tool("ffprobe"))

    def test_real_sources_cover_platforms(self):
        plan = self.prereqs.download_plan()
        self.assertTrue(all(a.url.startswith("https://") for a in plan))


class WindowsPolicyWorkaround(unittest.TestCase):
    def test_policy_block_detection(self):
        self.assertTrue(pathsetup.is_policy_block("[WinError 4551] An Application Control policy has blocked this file"))
        self.assertTrue(pathsetup.is_policy_block("[WinError 1260] This program is blocked by group policy"))
        self.assertFalse(pathsetup.is_policy_block("[WinError 2] The system cannot find the file specified"))

    def test_shim_runs_mp3cut_through_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = dict(os.environ)
            os.environ.update({"APPDATA": tmp, "LOCALAPPDATA": tmp, "XDG_DATA_HOME": tmp, "HOME": tmp})
            try:
                shim = pathsetup.win_create_shim()
                text = shim.read_bytes().decode("utf-8")
                self.assertEqual(shim.name, "mp3cut.cmd")
                self.assertIn(f'"{sys.executable}" -m mp3cutter %*\r\n', text)
                self.assertTrue(text.startswith("@echo off\r\n"))
                self.assertEqual(pathsetup._win_state()["shim"], str(shim))
            finally:
                os.environ.clear()
                os.environ.update(old)


class LinkHelpers(unittest.TestCase):
    def test_is_url(self):
        from mp3cutter import youtube
        for u in ("https://www.youtube.com/watch?v=abc", "http://youtu.be/abc", "youtu.be/abc",
                  "www.youtube.com/watch?v=x", "music.youtube.com/watch?v=x", "HTTPS://YOUTUBE.COM/shorts/x"):
            self.assertTrue(youtube.is_url(u), u)
        for f in ("song.mp3", "C:\\Music\\a.mp3", "/home/x/youtube.com.mp3", "-input"):
            self.assertFalse(youtube.is_url(f), f)
        self.assertEqual(youtube.normalize_url("youtu.be/abc"), "https://youtu.be/abc")

    def test_safe_filename(self):
        from mp3cutter import youtube
        self.assertEqual(youtube.safe_filename('AC/DC: "Back" <In> Black?*|'), "AC_DC_ _Back_ _In_ Black___")
        self.assertEqual(youtube.safe_filename("  dots and spaces ... "), "dots and spaces")
        self.assertEqual(youtube.safe_filename("CON"), "_CON")
        self.assertEqual(youtube.safe_filename("", "dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(len(youtube.safe_filename("x" * 400)), 150)
        self.assertEqual(youtube.safe_filename("Paravaledu Paravaledu | Telugu Song"), "Paravaledu Paravaledu _ Telugu Song")


def _install_fake_ytdlp(source_audio, info_overrides=None, calls=None):
    """Put a stand-in 'yt_dlp' module in sys.modules: 'downloads' by copying a local file."""
    import types
    calls = calls if calls is not None else []
    info = {"id": "abc123XYZ", "title": "Paravaledu Paravaledu", "duration": 200, "uploader": "Test Channel"}
    info.update(info_overrides or {})

    class YoutubeDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            calls.append(("extract", url, download, dict(self.opts)))
            if not download:
                return dict(info)
            tmpl = self.opts["outtmpl"]["default"]
            dst = Path(tmpl.replace("%(id)s", info["id"]).replace("%(ext)s", "webm"))
            shutil.copy(source_audio, dst)
            for h in self.opts.get("progress_hooks", []):
                h({"status": "finished"})
            return dict(info, requested_downloads=[{"filepath": str(dst)}])

        def prepare_filename(self, inf):
            return ""

    mod = types.ModuleType("yt_dlp")
    mod.YoutubeDL = YoutubeDL
    mod.parse_options = lambda args: types.SimpleNamespace(ydl_opts={"js_runtimes": {args[1].split(":")[0]: {}}})
    ver = types.ModuleType("yt_dlp.version")
    ver.__version__ = "2026.11.01"
    mod.version = ver
    ejs = types.ModuleType("yt_dlp_ejs")
    ejs.version = "0.3.2"
    sys.modules.update({"yt_dlp": mod, "yt_dlp.version": ver, "yt_dlp_ejs": ejs})
    return calls


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe not installed")
class LinkInput(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.src = cls.tmp / "source.opus.webm"
        subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=f=500:d=200:r=48000", "-ac", "2",
                        "-c:a", "libopus", "-b:a", "128k", "-f", "webm", str(cls.src)], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for m in ("yt_dlp", "yt_dlp.version", "yt_dlp_ejs"):
            sys.modules.pop(m, None)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.tmp))
        self.old_cwd = os.getcwd()
        os.chdir(self.work)
        os.environ["MP3CUT_DENO"] = sys.executable      # pretend a JS runtime exists; no download attempt

    def tearDown(self):
        os.chdir(self.old_cwd)
        os.environ.pop("MP3CUT_DENO", None)

    def test_link_cut_goes_to_output_folder_with_title(self):
        calls = _install_fake_ytdlp(self.src)
        url = "https://www.youtube.com/watch?v=abc123XYZ&list=PL123"
        self.assertEqual(main([url, "-s", "00:00", "-e", "00:13", "-q"]), 0)
        out = self.work / "output" / "Paravaledu Paravaledu_00m00s-00m13s.mp3"
        self.assertTrue(out.is_file())
        info = core.probe(out, FFPROBE)
        self.assertAlmostEqual(info.duration, 13, delta=0.2)
        self.assertEqual(info.audio_streams[0].codec, "mp3")
        dl = [c for c in calls if c[2]]
        self.assertEqual(len(dl), 1)
        self.assertTrue(dl[0][3]["noplaylist"])
        self.assertEqual(dl[0][3]["format"], "bestaudio/best")
        self.assertIn("js_runtimes", dl[0][3])
        # no temp download left behind next to the output
        self.assertEqual([p.name for p in (self.work / "output").iterdir()], [out.name])

    def test_link_dash_input_and_custom_output(self):
        _install_fake_ytdlp(self.src, {"title": 'Song: "Live" / 2026'})
        self.assertEqual(main(["-input", "youtu.be/abc123XYZ", "-start", "1:00", "-duration", "5",
                               "-o", str(self.work / "mine.mp3"), "-q"]), 0)
        self.assertTrue((self.work / "mine.mp3").is_file())
        self.assertFalse((self.work / "output").exists())

    def test_link_split(self):
        _install_fake_ytdlp(self.src)
        self.assertEqual(main(["https://youtu.be/abc123XYZ", "-s", "0", "-e", "1:00", "--parts", "3", "-q"]), 0)
        parts = sorted((self.work / "output" / "Paravaledu Paravaledu_parts").glob("*.mp3"))
        self.assertEqual(len(parts), 3)

    def test_link_range_checked_before_download(self):
        calls = _install_fake_ytdlp(self.src, {"duration": 60})
        self.assertEqual(main(["https://youtu.be/abc123XYZ", "-s", "2:00", "-e", "2:30", "-q"]), 1)
        self.assertFalse(any(c[2] for c in calls))

    def test_playlist_link_rejected(self):
        _install_fake_ytdlp(self.src, {"_type": "playlist", "entries": []})
        self.assertEqual(main(["https://www.youtube.com/playlist?list=PL123", "-q"]), 1)

    def test_link_info_does_not_download(self):
        import contextlib, io
        calls = _install_fake_ytdlp(self.src)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["https://youtu.be/abc123XYZ", "--info"]), 0)
        self.assertIn("Paravaledu Paravaledu", buf.getvalue())
        self.assertFalse(any(c[2] for c in calls))


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe not installed")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.wav = Path(cls.tmp) / "tone.wav"
        cls.mp4 = Path(cls.tmp) / "clip.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=200:r=48000",
                        "-ac", "2", str(cls.wav)], check=True)
        subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=d=20:s=320x240:r=10",
                        "-f", "lavfi", "-i", "sine=f=660:d=20", "-c:v", "mpeg4", "-c:a", "aac",
                        "-shortest", str(cls.mp4)], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _probe(self, p):
        return core.probe(Path(p), FFPROBE)

    def test_cut_wav(self):
        self.assertEqual(main([str(self.wav), "--start", "01:30", "--end", "02:45", "-q"]), 0)
        out = Path(self.tmp) / "tone_01m30s-02m45s.mp3"
        info = self._probe(out)
        self.assertAlmostEqual(info.duration, 75, delta=0.2)
        self.assertEqual(info.audio_streams[0].codec, "mp3")
        br = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "a:0", "-show_entries",
                             "stream=bit_rate", "-of", "csv=p=0", str(out)], capture_output=True, text=True)
        self.assertEqual(br.stdout.strip(), "320000")

    def test_video_extract(self):
        out = Path(self.tmp) / "x" / "audio.mp3"
        self.assertEqual(main(["--input", str(self.mp4), "-s", "5", "-d", "10", "-o", str(out), "-q"]), 0)
        info = self._probe(out)
        self.assertFalse(info.has_video)
        self.assertAlmostEqual(info.duration, 10, delta=0.2)

    def test_split(self):
        outdir = Path(self.tmp) / "parts"
        self.assertEqual(main([str(self.wav), "--parts", "4", "-o", str(outdir), "-q"]), 0)
        files = sorted(outdir.glob("*.mp3"))
        self.assertEqual(len(files), 4)
        for f in files:
            self.assertAlmostEqual(self._probe(f).duration, 50, delta=0.2)

    def test_split_every_folds_tiny_remainder(self):
        outdir = Path(self.tmp) / "every"
        self.assertEqual(main([str(self.wav), "-e", "120.3", "--split-every", "1:00", "-o", str(outdir), "-q"]), 0)
        files = sorted(outdir.glob("*.mp3"))
        self.assertEqual(len(files), 2)
        self.assertAlmostEqual(self._probe(files[-1]).duration, 60.3, delta=0.2)

    def test_single_dash_long_options_and_odd_names(self):
        odd = Path(self.tmp) / "Paravaledu Paravaledu [320kbps].wav"
        shutil.copy(self.wav, odd)
        self.assertEqual(main(["-input", str(odd), "-start", "00:00", "-end", "00:13", "-q"]), 0)
        self.assertTrue((Path(self.tmp) / "Paravaledu Paravaledu [320kbps]_00m00s-00m13s.mp3").exists())
        self.assertEqual(main(["-input=" + str(odd), "-s", "5", "-duration", "3", "-q"]), 0)

    def test_errors(self):
        self.assertEqual(main([str(self.wav), "--start", "10:00", "-q"]), 1)       # beyond end
        self.assertEqual(main([str(self.wav), "-s", "30", "-e", "10", "-q"]), 1)   # end before start
        self.assertEqual(main([str(Path(self.tmp) / "missing.mp3"), "-q"]), 1)
        same = Path(self.tmp) / "same.mp3"
        self.assertEqual(main([str(self.wav), "-d", "2", "-o", str(same), "-q"]), 0)
        self.assertEqual(main([str(same), "-o", str(same), "--overwrite", "-q"]), 1)  # would overwrite input


if __name__ == "__main__":
    unittest.main()
