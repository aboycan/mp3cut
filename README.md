**** Beta phase: tested on macOS and Windows; Linux not tested ****

# mp3cut

Cut, split or extract audio from **any audio or video file** (mp3, wav, flac, m4a, aac, ogg, opus, wma,
mp4, mkv, mov, avi, webm, …) **or a YouTube link** into **MP3, 320 kbps CBR**. Runs as a plain `mp3cut` command from any
folder on Windows 10/11, macOS (Intel and Apple Silicon) and Linux.

```
mp3cut --input song.mp3 --start 01:30 --end 02:45
mp3cut "https://www.youtube.com/watch?v=VIDEO_ID" --start 01:30 --end 02:45
```

## Prerequisites: handled by the installer

You don't need to install anything first. The installer checks for what's missing and installs it:

| Prerequisite | If missing, the installer… |
|---|---|
| **Python 3.8+** | macOS: installs it with Homebrew (`brew install python`), or starts Apple's Command Line Tools installer. Windows: installs Python for your user with `winget` (no admin). Linux: offers to run your package manager (`apt`/`dnf`/`pacman`/`zypper`/`apk`). This is the only step that can need `sudo`. |
| **pip** | Runs `ensurepip`. On Linux, falls back to your package manager. |
| **yt-dlp** (for YouTube links) | Installed by pip together with mp3cut (`yt-dlp[default]`, which includes `yt-dlp-ejs`). |
| **Deno** (JavaScript runtime YouTube needs) | Downloads a **private copy** (SHA-256 verified, no admin). Node.js is used as a fallback if you decline. |
| **FFmpeg + FFprobe** (with the MP3 encoder) | Downloads a **private copy just for mp3cut**: no admin rights, and nothing changes system-wide. The SHA-256 checksum is verified and the binaries are test-run before use. |

The private tools are stored under `…/mp3cut/ffmpeg/` and `…/mp3cut/deno/` in:

| OS | Folder | Source |
|---|---|---|
| macOS (Intel + Apple Silicon) | `~/Library/Application Support/mp3cut/ffmpeg/` | ffmpeg.martin-riedl.de static builds |
| Linux (x86_64 + ARM64) | `~/.local/share/mp3cut/ffmpeg/` | ffmpeg.martin-riedl.de static builds |
| Windows (x64 + ARM64) | `%LOCALAPPDATA%\mp3cut\ffmpeg\` | github.com/BtbN/FFmpeg-Builds |

If FFmpeg is already on your system and has the MP3 encoder, it's used as-is and nothing is downloaded.
mp3cut looks for FFmpeg in this order: `MP3CUT_FFMPEG`/`MP3CUT_FFPROBE` variables, then the private
copy, then PATH.

- `mp3cut --install-ffmpeg` downloads or updates the private copy at any time.
- If FFmpeg is missing when you run a cut, mp3cut offers to download it on the spot.
- For a company mirror or offline install, set `MP3CUT_FFMPEG_URL` (and `MP3CUT_FFPROBE_URL`) to zip/tar archives.

mp3cut needs no third-party Python packages.

## Install

| Platform        | Command (run inside this folder) |
|-----------------|----------------------------------|
| macOS / Linux   | `./install.sh` (or `bash install.sh`) |
| Windows         | `install.bat` (double-click, or run it in cmd/PowerShell) |
| Any (manual)    | `python -m pip install --user .` then `python -m mp3cutter.pathsetup install` |

The installer:

1. Finds Python 3.8+ and pip, installing them if they're missing (see above).
2. Runs `python -m pip install --user .`, which registers the `mp3cut` console script
   (`[project.scripts] mp3cut = "mp3cutter.cli:main"`) in your **user-level** scripts folder.
   No admin rights or sudo.
3. Finds where pip actually put `mp3cut` (from the package's install record, not a guessed path) and
   checks whether that folder is on PATH.
4. If it isn't, it asks before adding it:
   - **Windows**: appends the folder to your *user* PATH (`HKCU\Environment`), keeps every existing
     entry, skips duplicates, notifies Windows of the change and re-reads the registry to confirm it.
     System PATH is never touched.
   - **macOS / Linux**: adds a short marked block to `~/.zshrc` (zsh) or `~/.bashrc` /
     `~/.bash_profile` (bash). The block only adds the folder if it isn't already on PATH. A backup
     (`*.mp3cut.bak`) is saved first, and re-running updates the same block rather than adding another.
5. If FFmpeg is missing or has no MP3 encoder, downloads the private copy.
6. Checks that `mp3cut --version` and `mp3cut --help` work, that FFmpeg/FFprobe and the MP3 encoder
   are present, and prints `MP3CUT INSTALLATION SUCCESSFUL`.

Open a **new terminal** afterwards (or `source ~/.zshrc`) if PATH was changed.

Re-running the installer is safe. Use `--yes` / `/y` to skip prompts.

**PEP 668 note:** Homebrew Python and Debian/Ubuntu 23.04+ refuse plain `pip install --user`.
mp3cut has no dependencies, so `install.sh` retries with `--break-system-packages`. This only
adds mp3cut under your home folder.

## Usage

```
mp3cut --input song.mp3 --start 01:30 --end 02:45       # cut a section
mp3cut song.mp3 -s 1:30 -d 45                           # 45 s starting at 1:30
mp3cut movie.mp4                                        # extract the whole audio track -> movie.mp3
mp3cut movie.mkv --start 1:02:10 --end 1:05:00          # cut audio straight from a video
mp3cut movie.mkv --info                                 # list audio tracks
mp3cut movie.mkv --audio-stream 1 -s 10:00 -d 2m        # use the 2nd audio track
mp3cut podcast.m4a --split-every 10:00                  # 10-minute parts -> podcast_parts/
mp3cut concert.flac --parts 4 --output parts/           # 4 equal parts
mp3cut talk.wav -s 5 -o ~/Music/talk-intro.mp3          # choose the output name
```

| Option | Meaning |
|---|---|
| `-i, --input FILE` (or positional) | Any audio/video file FFmpeg can read |
| `-s, --start TIME` / `-e, --end TIME` / `-d, --duration TIME` | Range. Formats: `90`, `90.5`, `1:30`, `01:30.250`, `1:02:03`, `1m30s` |
| `-o, --output PATH` | Output file or folder. Default: next to the input, e.g. `song_01m30s-02m45s.mp3` |
| `--split-every TIME` / `--parts N` | Split the range into several MP3s |
| `-b, --bitrate` | Default `320k` |
| `-a, --audio-stream N` | Pick an audio track in multi-track videos (see `--info`) |
| `--overwrite` | Replace existing outputs (default writes `name (1).mp3`) |
| `-q, --quiet` | Print only the created file paths |

Output details: MP3 via libmp3lame, constant 320 kbps, 32/44.1/48 kHz (other sample rates are
resampled, because MPEG-1 Layer III needs one of these for 320 kbps), surround is downmixed to stereo,
tags are copied, video/subtitles are dropped. mp3cut encodes to a temp file and renames it when done,
so a failed or cancelled run never leaves a half-written MP3, and it refuses to overwrite its own input.

## YouTube links

Put the link in quotes, because `&` and `?` mean something special to the shell. Every other option works the same:

```
mp3cut "https://www.youtube.com/watch?v=VIDEO_ID" -s 01:30 -e 02:45
mp3cut "https://youtu.be/VIDEO_ID" -s 1:30 -d 45          # 45 s from 1:30
mp3cut "https://youtu.be/VIDEO_ID"                        # whole audio
mp3cut "https://youtu.be/VIDEO_ID" --split-every 5:00     # 5-minute parts
mp3cut "https://youtu.be/VIDEO_ID" --info                 # title + length, no download
mp3cut "https://youtu.be/VIDEO_ID" -s 10 -e 20 -o D:\Music\clip.mp3
```

- **Output:** `output\<video title>_<start>-<end>.mp3` in the folder you run the command from, for
  example `output\Paravaledu Paravaledu_00m00s-00m13s.mp3`. Characters that aren't allowed in file
  names are replaced with `_`. Split parts go into `output\<title>_parts\`. `-o` overrides the location.
- **Only the audio stream is downloaded**, to a temporary folder that's deleted afterwards. The range is
  checked against the video's length *before* downloading.
- A link with `&list=…` uses just that one video. Playlist-only links are rejected.
- **Quality:** YouTube audio is usually 128–160 kbps (Opus/AAC). The 320 kbps MP3 keeps everything in
  it but can't add quality that isn't there.
- **If YouTube downloads start failing** (YouTube changes often), run `mp3cut --update-ytdlp`. It
  updates yt-dlp and the private Deno.
- Only download content you have the right to use, and respect YouTube's Terms of Service.

## Troubleshooting

```
mp3cut --check-env
```

This shows the OS, CPU architecture, Python version and executable, where `mp3cut` is installed,
whether that folder is on PATH, the FFmpeg/FFprobe paths and versions, MP3 encoder availability and
module status. If something is wrong it prints the exact command to fix it.

If `mp3cut` isn't found yet:

```
python3 -m mp3cutter --check-env              # always works, PATH or not
python3 -m mp3cutter.pathsetup install        # (re)configure PATH
```

To use FFmpeg from a non-standard location, set `MP3CUT_FFMPEG` and `MP3CUT_FFPROBE`.
If FFmpeg is missing or broken, run `mp3cut --install-ffmpeg`. For YouTube problems, run
`mp3cut --update-ytdlp`. `mp3cut --check-env` also shows the yt-dlp, yt-dlp-ejs and Deno status.

## Uninstall

```
python -m pip uninstall mp3cut
```

To also remove the PATH change the installer made, run `./uninstall.sh` or `uninstall.bat`, or run
`mp3cut --remove-path` before uninstalling. It asks first, and it only removes the marked block or
the single PATH entry the installer added, plus (if you confirm) mp3cut's private FFmpeg folder. It
never deletes MP3s, other PATH entries, a system FFmpeg or other files.

`pip uninstall mp3cut` leaves yt-dlp installed, because other tools may use it. Remove it with
`python -m pip uninstall yt-dlp yt-dlp-ejs` if you don't need it.

## Development

```
python -m unittest discover -s tests
```
