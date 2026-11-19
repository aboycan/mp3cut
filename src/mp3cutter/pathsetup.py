"""User-level PATH configuration and post-install verification for ``mp3cut``.

Usage (the installers call this; it is safe to re-run - it is idempotent):

    python -m mp3cutter.pathsetup install [--yes] [--no-path-change]
    python -m mp3cutter.pathsetup remove  [--yes]
    python -m mp3cutter.pathsetup status

Rules: never needs admin/sudo, never touches system-wide settings, never
removes or reorders existing PATH entries, never adds duplicates.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from mp3cutter import __version__
from mp3cutter import platform_info as pi
from mp3cutter.ffmpeg import find_tool, has_mp3_encoder, install_hint, tool_origin, tool_version

BLOCK_BEGIN = "# >>> mp3cut PATH (added by the mp3cut installer; safe to remove) >>>"
BLOCK_END = "# <<< mp3cut PATH <<<"
_BLOCK_RE = re.compile(r"\n?" + re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END) + r"[^\n]*\n?", re.S)


# --------------------------------------------------------------------- prompts
def ask(question: str, assume_yes: bool, default: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        return default
    hint = "Y/n" if default else "y/N"
    try:
        ans = input(f"{question} [{hint}] ").strip().lower()
    except EOFError:
        return default
    return default if not ans else ans in ("y", "yes")


def _home_display(p: Path) -> str:
    try:
        return "~/" + p.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(p)


# ======================================================================= POSIX
def _dq(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


def _shell_dir_expr(d: Path) -> str:
    """Double-quoted shell expression for d, using $HOME when d is inside the home folder."""
    try:
        rel = d.relative_to(Path.home()).as_posix()
        return '"$HOME"' if rel == "." else f'"$HOME/{_dq(rel)}"'
    except ValueError:
        return f'"{_dq(str(d))}"'


def posix_block(d: Path) -> str:
    q = _shell_dir_expr(d)
    return (f"{BLOCK_BEGIN}\n"
            f'case ":$PATH:" in\n'
            f"  *:{q}:*) ;;\n"
            f'  *) export PATH={q[:-1]}:$PATH" ;;\n'
            f"esac\n"
            f"{BLOCK_END}\n")


def posix_rc_targets(shell: str) -> List[Path]:
    """Shell start-up files to update for the given shell (empty = unsupported shell)."""
    home = Path.home()
    mac = pi.os_name() == "macOS"
    if shell == "zsh":
        return [Path(os.environ.get("ZDOTDIR") or home) / ".zshrc"]
    if shell == "bash":
        targets: List[Path] = []
        bashrc = home / ".bashrc"
        if not mac or bashrc.exists():
            targets.append(bashrc)                       # interactive non-login shells (Linux terminals)
        login = next((home / n for n in (".bash_profile", ".bash_login", ".profile") if (home / n).exists()), None)
        if mac:
            targets.append(login or home / ".bash_profile")  # Terminal.app starts login shells
        elif login is not None and login.name != ".profile":
            targets.append(login)                        # .bash_profile hides .profile/.bashrc for login shells
        return targets
    if shell in ("sh", "dash", "ksh", "mksh", "ash"):
        return [home / ".profile"]
    return []


def _all_rc_candidates() -> List[Path]:
    home = Path.home()
    names = [".zshrc", ".bashrc", ".bash_profile", ".bash_login", ".profile"]
    cands = [home / n for n in names]
    zdot = os.environ.get("ZDOTDIR")
    if zdot:
        cands.insert(0, Path(zdot) / ".zshrc")
    return cands


def _write_preserving(rc: Path, text: str) -> None:
    target = rc.resolve() if rc.is_symlink() else rc     # keep dotfile-manager symlinks intact
    if target.exists():
        backup = target.with_name(target.name + ".mp3cut.bak")
        backup.write_bytes(target.read_bytes())
        mode = target.stat().st_mode
    else:
        mode = None
    tmp = target.with_name(target.name + ".mp3cut.tmp")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode & 0o7777)
    os.replace(tmp, target)


def upsert_block(rc: Path, block: str) -> bool:
    """Insert or update our marked block in rc. Returns True if the file changed."""
    old = rc.read_text(encoding="utf-8", errors="surrogateescape") if rc.exists() else ""
    if _BLOCK_RE.search(old):
        new = _BLOCK_RE.sub(lambda _m: "\n" + block, old, count=1)
        if new.startswith("\n") and not old.startswith("\n"):
            new = new[1:]
    else:
        sep = "" if not old or old.endswith("\n\n") else ("\n" if old.endswith("\n") else "\n\n")
        new = old + sep + block
    if new == old:
        return False
    rc.parent.mkdir(parents=True, exist_ok=True)
    _write_preserving(rc, new)
    return True


def remove_block(rc: Path) -> bool:
    if not rc.exists():
        return False
    old = rc.read_text(encoding="utf-8", errors="surrogateescape")
    new = _BLOCK_RE.sub("\n", old)
    new = re.sub(r"\n{3,}$", "\n", new)
    if new == old:
        return False
    _write_preserving(rc, new)
    return True


def posix_block_files() -> List[Path]:
    out = []
    for rc in _all_rc_candidates():
        try:
            if rc.exists() and BLOCK_BEGIN in rc.read_text(encoding="utf-8", errors="replace"):
                out.append(rc)
        except OSError:
            pass
    return out


# ===================================================================== Windows
_WIN_ENV_KEY = "Environment"
_WIN_SYS_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"


def _win_read(root_name: str = "user") -> Tuple[str, int]:
    import winreg
    root, sub = ((winreg.HKEY_CURRENT_USER, _WIN_ENV_KEY) if root_name == "user"
                 else (winreg.HKEY_LOCAL_MACHINE, _WIN_SYS_KEY))
    try:
        with winreg.OpenKey(root, sub, 0, winreg.KEY_READ) as k:
            val, typ = winreg.QueryValueEx(k, "Path")
            return str(val), typ
    except OSError:
        return "", winreg.REG_EXPAND_SZ


def _win_write_user(value: str, typ: int) -> None:
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _WIN_ENV_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "Path", 0, typ, value)
    _win_broadcast()


def _win_broadcast() -> None:
    """Tell Explorer & new terminals that the environment changed (no reboot needed)."""
    try:
        import ctypes
        result = ctypes.c_size_t()
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000,
                                                 ctypes.byref(result))
    except Exception:
        pass


def _state_file() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "mp3cut" / "install-state.json"


def _win_registry_has(d: Path) -> bool:
    user, _ = _win_read("user")
    system, _ = _win_read("system")
    return pi.dir_in_path_value(d, user) or pi.dir_in_path_value(d, system)


def win_add(d: Path) -> Tuple[bool, str]:
    """Append d to the *user* PATH in the registry. Returns (changed, message)."""
    value, typ = _win_read("user")
    if pi.dir_in_path_value(d, value):
        return False, "already in user PATH"
    entry = str(d)
    new_value = (value.rstrip(";") + ";" + entry) if value.strip() else entry
    _win_write_user(new_value, typ)
    check, _ = _win_read("user")
    if not pi.dir_in_path_value(d, check):
        return False, "registry update could not be verified"
    sf = _state_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({"path_entry_added": entry, "version": __version__}, indent=2), encoding="utf-8")
    return True, "added to user PATH (HKCU\\Environment)"


def win_remove(assume_yes: bool) -> Tuple[bool, str]:
    sf = _state_file()
    if not sf.exists():
        return False, "no PATH entry was added by the mp3cut installer"
    try:
        entry = json.loads(sf.read_text(encoding="utf-8")).get("path_entry_added")
    except (OSError, ValueError):
        entry = None
    if not entry:
        return False, "install state unreadable; leaving PATH unchanged"
    value, typ = _win_read("user")
    parts = [p for p in value.split(";")]
    target = pi._norm(entry)
    keep = [p for p in parts if not (p.strip() and pi._norm(p) == target)]
    if len(keep) == len(parts):
        sf.unlink()
        return False, f"{entry} is no longer in the user PATH"
    if not ask(f"Remove '{entry}' from your user PATH? (other Python tools installed there would also "
               f"stop being found)", assume_yes, default=False):
        return False, "left PATH unchanged"
    _win_write_user(";".join(keep), typ)
    sf.unlink()
    return True, f"removed {entry} from user PATH"


# ================================================================ verification
def _run(cmd: List[str]) -> Tuple[bool, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        return p.returncode == 0, (p.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def _effective_path(d: Path) -> str:
    """PATH as a *new* terminal will see it after our change."""
    cur = os.environ.get("PATH", "")
    if pi.dir_in_path_value(d, cur):
        return cur
    return (cur + os.pathsep + str(d)) if os.name == "nt" else (str(d) + os.pathsep + cur)


def install_flow(assume_yes: bool = False, change_path: bool = True, install_ffmpeg_prereq: bool = True) -> int:
    system = pi.os_name()
    print(f"\nConfiguring mp3cut {__version__} on {system} {pi.os_version()} ({pi.architecture()})")
    print(f"Python  : {sys.version.split()[0]}  {sys.executable}")

    exe = pi.installed_executable()
    if exe is None:
        print("\nERROR: the mp3cut package is not installed for this Python.\n"
              f"Run:  \"{sys.executable}\" -m pip install --user .   (from the mp3cut folder)")
        return 1
    scripts_dir = exe.parent
    print(f"Command : {exe}")

    problems: List[str] = []
    notes: List[str] = []
    new_terminal = False
    path_status = "Configured"

    # ---- PATH
    if pi.dir_in_path_value(scripts_dir):
        path_status = "Configured (already on PATH)"
    elif system == "Windows":
        if _win_registry_has(scripts_dir):
            path_status, new_terminal = "Configured (already in user PATH)", True
        elif not change_path:
            path_status = "Not configured (skipped)"
            problems.append(f"Add {scripts_dir} to your user PATH, or re-run install.bat")
        elif ask(f"{scripts_dir} is not on your PATH. Add it to your user PATH?", assume_yes, default=True):
            changed, msg = win_add(scripts_dir)
            if changed:
                path_status, new_terminal = "Configured (user PATH updated)", True
            else:
                path_status = f"NOT configured ({msg})"
                problems.append(f"Add {scripts_dir} to your user PATH manually "
                                "(Settings > System > About > Advanced system settings > Environment Variables)")
        else:
            path_status = "Not configured (declined)"
            problems.append(f"Add {scripts_dir} to your user PATH to run 'mp3cut' from anywhere")
    else:
        shell = pi.detect_shell()
        targets = posix_rc_targets(shell)
        if not targets:
            path_status = f"Not configured (shell '{shell}' not auto-configured)"
            if shell == "fish":
                problems.append(f"Run:  fish_add_path {scripts_dir}")
            else:
                problems.append(f"Add {scripts_dir} to PATH in your {shell} start-up file")
        elif not change_path:
            path_status = "Not configured (skipped)"
            problems.append(f"Add to {_home_display(targets[0])}:  export PATH=\"{scripts_dir}:$PATH\"")
        else:
            already = [rc for rc in targets if rc.exists() and posix_block(scripts_dir) in
                       rc.read_text(encoding="utf-8", errors="replace")]
            if len(already) == len(targets):
                path_status = f"Configured ({', '.join(_home_display(t) for t in targets)})"
                new_terminal = True
            elif ask(f"{scripts_dir} is not on your PATH. Add it to {', '.join(_home_display(t) for t in targets)}?",
                     assume_yes, default=True):
                try:
                    for rc in targets:
                        upsert_block(rc, posix_block(scripts_dir))
                    path_status = f"Configured ({', '.join(_home_display(t) for t in targets)})"
                    new_terminal = True
                except OSError as exc:
                    path_status = f"NOT configured ({exc})"
                    problems.append(f"Add to {_home_display(targets[0])}:  export PATH=\"{scripts_dir}:$PATH\"")
            else:
                path_status = "Not configured (declined)"
                problems.append(f"Add to {_home_display(targets[0])}:  export PATH=\"{scripts_dir}:$PATH\"")
        if new_terminal:
            notes.append(f"Open a new terminal (or run:  source {_home_display(targets[0])})  before using mp3cut.")

    # ---- command registration
    ok_exe = exe.is_file() and (os.name == "nt" or os.access(exe, os.X_OK))
    if not ok_exe:
        problems.append(f"{exe} is missing or not executable")
    eff = _effective_path(scripts_dir) if not path_status.startswith(("Not", "NOT")) else os.environ.get("PATH", "")
    found = pi.which_mp3cut(eff)
    if found is None:
        problems.append("'mp3cut' is not reachable through PATH yet")
    elif os.path.normcase(os.path.realpath(found)) != os.path.normcase(os.path.realpath(str(exe))):
        problems.append(f"another 'mp3cut' comes first on PATH: {found}  (remove it or reorder PATH)")
    ok_ver, ver_out = _run([str(exe), "--version"])
    ok_help, _ = _run([str(exe), "--help"])
    if not (ok_ver and __version__ in ver_out):
        problems.append(f"'mp3cut --version' failed: {ver_out[:200]}")
    if not ok_help:
        problems.append("'mp3cut --help' failed")

    # ---- ffmpeg (prerequisite: install mp3cut's private copy when missing or unusable)
    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    reason = None
    if not (ffmpeg and ffprobe):
        reason = "\nFFmpeg/FFprobe (required by mp3cut) were not found."
    elif not has_mp3_encoder(ffmpeg):
        reason = f"\nThe FFmpeg at {ffmpeg} has no MP3 encoder (libmp3lame)."
    if reason and install_ffmpeg_prereq:
        from mp3cutter.prereqs import offer_install
        if offer_install(assume_yes, reason=reason, non_interactive_default=True, log=print):
            ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    if ffmpeg and ffprobe:
        fv, pv = tool_version(ffmpeg), tool_version(ffprobe)
        if has_mp3_encoder(ffmpeg):
            ff_status = f"Available (ffmpeg {fv}, {tool_origin(ffmpeg)})"
        else:
            ff_status = f"Found {fv} but it has NO MP3 encoder (libmp3lame)"
            problems.append("Install a full FFmpeg build. " + install_hint())
    else:
        ff_status = "NOT FOUND: " + " and ".join(n for n, v in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not v)
        problems.append(install_hint())

    deps_status = "Verified (standard library only)" if ok_ver else "NOT verified"
    install_kind = "Virtual environment" if pi.in_venv() else "User-level"

    success = ok_exe and ok_ver and ok_help and found is not None and not problems
    bar = "=" * 46
    title = "MP3CUT INSTALLATION SUCCESSFUL" if success else (
        "MP3CUT INSTALLED - ACTION NEEDED" if ok_ver else "MP3CUT INSTALLATION FAILED")
    print(f"\n{bar}\n  {title}\n{bar}\n")
    print("Command:\n  mp3cut\n")
    print(f"Installation:\n  {install_kind}  ({exe})\n")
    print(f"Platform:\n  {system} {pi.os_version()}, {pi.architecture()}, Python {sys.version.split()[0]}\n")
    print(f"PATH:\n  {path_status}\n")
    print(f"FFmpeg:\n  {ff_status}\n")
    print(f"Dependencies:\n  {deps_status}\n")
    if problems:
        print("To do:")
        for p in problems:
            print(f"  - {p}")
        print()
    for n in notes:
        print(f"NOTE: {n}")
    if system == "Windows" and new_terminal:
        print("NOTE: Close this window and open a NEW terminal for the PATH change to take effect.")
    print("Try:\n  mp3cut --help\n  mp3cut --check-env\n")
    return 0 if ok_ver else 1


def remove_flow(assume_yes: bool = False) -> int:
    """Undo only the PATH changes this installer made. Never touches user files."""
    if os.name == "nt":
        changed, msg = win_remove(assume_yes)
        print(msg)
    else:
        files = posix_block_files()
        if not files:
            print("No mp3cut PATH block found in your shell start-up files; nothing to remove.")
        elif ask("Remove the mp3cut PATH block from " + ", ".join(_home_display(f) for f in files) + "?",
                 assume_yes, default=False):
            for f in files:
                if remove_block(f):
                    print(f"Removed mp3cut PATH block from {_home_display(f)} (backup: {f.name}.mp3cut.bak)")
        else:
            print("Left shell start-up files unchanged.")
    from mp3cutter.prereqs import managed_dir, remove_managed
    if managed_dir().exists():
        if ask(f"Remove mp3cut's private FFmpeg copy ({managed_dir()})?", assume_yes, default=False):
            remove_managed()
            print(f"Removed {managed_dir()}")
        else:
            print(f"Kept {managed_dir()}")
    print(f"\nTo uninstall the package:  \"{sys.executable}\" -m pip uninstall mp3cut")
    print("Your MP3 files are never deleted by mp3cut.")
    return 0


def status() -> int:
    exe = pi.installed_executable()
    print(f"executable : {exe or 'not installed'}")
    if exe:
        print(f"on PATH    : {pi.dir_in_path_value(exe.parent)}")
    if os.name != "nt":
        print(f"shell      : {pi.detect_shell()}  -> {[_home_display(t) for t in posix_rc_targets(pi.detect_shell())]}")
        print(f"PATH blocks: {[_home_display(f) for f in posix_block_files()]}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m mp3cutter.pathsetup")
    p.add_argument("action", choices=("install", "remove", "status"))
    p.add_argument("--yes", "-y", action="store_true", help="do not prompt")
    p.add_argument("--no-path-change", action="store_true", help="verify only, do not edit PATH")
    p.add_argument("--no-ffmpeg", action="store_true", help="do not download FFmpeg if it is missing")
    a = p.parse_args(argv)
    if a.action == "install":
        return install_flow(assume_yes=a.yes, change_path=not a.no_path_change,
                            install_ffmpeg_prereq=not a.no_ffmpeg)
    if a.action == "remove":
        return remove_flow(assume_yes=a.yes)
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
