"""Allows ``python -m mp3cutter ...`` as a fallback when ``mp3cut`` is not on PATH."""

from mp3cutter.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
