#!/usr/bin/env bash
# mp3cut installer for macOS (Intel + Apple Silicon) and Linux.
# Installs everything mp3cut needs: Python (only if missing), the mp3cut command,
# PATH setup, and a private FFmpeg copy (only if FFmpeg is missing).
# User-level install: no sudo, except Linux's package manager if Python itself is missing. Safe to re-run.
#
#   ./install.sh          interactive
#   ./install.sh --yes    accept all prompts (e.g. for scripts)
#   PYTHON=/path/to/python3 ./install.sh   use a specific interpreter
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
YES_FLAG=""
for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES_FLAG="--yes" ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

info() { printf '==> %s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then
  echo "WARNING: running as root installs mp3cut for root only. Run as your normal user instead." >&2
fi

confirm() {   # confirm "question" -> 0 for yes (default yes)
  [ -n "$YES_FLAG" ] && return 0
  [ -t 0 ] || return 1
  local a; read -r -p "$1 [Y/n] " a || return 1
  case "$a" in ""|y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

find_python() {
  PY="${PYTHON:-}"
  [ -n "$PY" ] && return 0
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
      PY="$(command -v "$c")"; return 0
    fi
  done
  if command -v brew >/dev/null 2>&1; then   # just-installed Homebrew Python, located via brew itself
    local c; c="$(brew --prefix 2>/dev/null)/bin/python3"
    if [ -x "$c" ] && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
      PY="$c"; return 0
    fi
  fi
  return 1
}

linux_pkg_cmd() {   # $1 = what: "python" or "pip"
  local py="python3 python3-pip"; [ "$1" = pip ] && py="python3-pip"
  if   command -v apt-get >/dev/null 2>&1; then echo "sudo apt-get update && sudo apt-get install -y $py"
  elif command -v dnf     >/dev/null 2>&1; then echo "sudo dnf install -y $py"
  elif command -v yum     >/dev/null 2>&1; then echo "sudo yum install -y $py"
  elif command -v zypper  >/dev/null 2>&1; then echo "sudo zypper install -y $py"
  elif command -v pacman  >/dev/null 2>&1; then [ "$1" = pip ] && echo "sudo pacman -S --needed --noconfirm python-pip" || echo "sudo pacman -S --needed --noconfirm python python-pip"
  elif command -v apk     >/dev/null 2>&1; then [ "$1" = pip ] && echo "sudo apk add py3-pip" || echo "sudo apk add python3 py3-pip"
  fi
}

install_system_python() {   # $1 = "python" or "pip"
  case "$(uname -s)" in
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        confirm "Python 3.8+ is required. Install it with Homebrew ('brew install python', no sudo)?" &&
          brew install python && return 0
      else
        info "Python 3 comes with Apple's Command Line Tools. Starting their installer..."
        xcode-select --install 2>/dev/null || true
        fail "Finish the 'Command Line Tools' installation in the dialog that opened, then run ./install.sh again."
      fi ;;
    Linux)
      local cmd; cmd="$(linux_pkg_cmd "$1")"
      [ -n "$cmd" ] || return 1
      info "This needs your distribution's package manager (the only step that uses sudo):"
      info "  $cmd"
      confirm "Run it now?" && eval "$cmd" && return 0 ;;
  esac
  return 1
}

# ---- 1. Python >= 3.8 (installed automatically if missing) ------------------
if ! find_python; then
  install_system_python python && find_python ||
    fail "Python 3.8+ is required. Install it (macOS: brew install python, Linux: $(linux_pkg_cmd python)) and re-run."
fi
info "Using Python: $PY ($("$PY" -c 'import sys; print(sys.version.split()[0])'))"

# ---- 2. pip (installed automatically if missing) ----------------------------
if ! "$PY" -m pip --version >/dev/null 2>&1; then
  info "pip not found; trying ensurepip"
  "$PY" -m ensurepip --user >/dev/null 2>&1 || "$PY" -m ensurepip >/dev/null 2>&1 || true
  if ! "$PY" -m pip --version >/dev/null 2>&1; then
    install_system_python pip || true
    "$PY" -m pip --version >/dev/null 2>&1 ||
      fail "pip is required. Install it (Linux: $(linux_pkg_cmd pip)) and re-run."
  fi
fi

# ---- 3. pip install --user . ------------------------------------------------
IN_VENV="$("$PY" -c 'import sys; print(int(sys.prefix != getattr(sys, "base_prefix", sys.prefix)))')"
PIP_ARGS=(install --upgrade --no-warn-script-location)
if [ "$IN_VENV" = "0" ]; then
  PIP_ARGS+=(--user)
else
  info "Active virtual environment detected; installing into it (no --user)"
fi

LOG="$(mktemp "${TMPDIR:-/tmp}/mp3cut-pip.XXXXXX")"
trap 'rm -f "$LOG"' EXIT
info "Installing mp3cut: $PY -m pip ${PIP_ARGS[*]} ."
if ! "$PY" -m pip "${PIP_ARGS[@]}" "$SCRIPT_DIR" 2>&1 | tee "$LOG"; then
  if grep -qi "externally-managed-environment" "$LOG"; then
    # Homebrew Python and Debian/Ubuntu 23.04+ mark the interpreter as externally managed (PEP 668).
    # mp3cut has no third-party dependencies, so a --user install only adds mp3cut itself under
    # your home folder and cannot break system packages.
    info "This Python is 'externally managed' (PEP 668). mp3cut has no dependencies, so installing"
    info "it into your user folder is safe; retrying with --break-system-packages."
    "$PY" -m pip "${PIP_ARGS[@]}" --break-system-packages "$SCRIPT_DIR" || fail "pip install failed (see above)"
  elif grep -qi "requirement setuptools" "$LOG" &&
       "$PY" -c 'import setuptools, sys; sys.exit(0 if int(setuptools.__version__.split(".")[0]) >= 61 else 1)' 2>/dev/null; then
    info "PyPI is unreachable; building with the locally installed setuptools instead."
    "$PY" -m pip "${PIP_ARGS[@]}" --no-build-isolation "$SCRIPT_DIR" || fail "pip install failed (see above)"
  elif grep -qi "requirement setuptools" "$LOG"; then
    fail "pip could not download 'setuptools' (no internet / proxy?). Connect to the internet and re-run."
  else
    fail "pip install failed (see above)"
  fi
fi

# ---- 4. PATH configuration + verification -----------------------------------
# run from $HOME so the installed package (not the source tree) is imported
cd "$HOME"
"$PY" -m mp3cutter.pathsetup install $YES_FLAG
