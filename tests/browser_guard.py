#!/usr/bin/env python3
"""Make sure the Playwright system libraries are there BEFORE a suite launches.

Why this exists. The browser binaries persist across container recreates; their
shared objects (`/usr/lib`) do not. So a sandbox upgrade silently leaves both
engines unlaunchable, and the suite dies mid-run with a wall of loader noise
that names a missing `.so` rather than the thing to do about it. That happened
on 2026-08-19 (chromium: libglib-2.0.so.0; webkit: ~10 libs), and the repair
command existed all along -- it was simply not reached for.

Design notes:
  * IDEMPOTENT twice over -- a module-level flag makes repeated calls in one
    process free, and the underlying check is a read-only `ldd` that changes
    nothing when everything is healthy (a few ms).
  * Repair is OPTIONAL and environment-driven: set `BROWSER_ENSURE_CMD` to a
    command that installs the system deps. Without it the guard still runs, it
    just reports what is missing instead of fixing it -- so a fork, a CI job or
    a laptop gets a useful message and never an unexpected privileged command.
  * It VERIFIES AFTER repairing rather than trusting the command's exit code.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

_CHECKED = False

_BROWSER_BINARIES = ("chrome-headless-shell", "headless_shell", "Playwright")


def _cache_dir() -> Path:
    return Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        or (Path.home() / ".cache" / "ms-playwright")
    )


def _broken_engines(cache: Path):
    """Return [(binary, count of missing .so)] for every engine present."""
    if not shutil.which("ldd"):
        return []  # cannot tell; stay out of the way rather than block the run
    broken = []
    for path in sorted(cache.glob("*/**/*")):
        if not path.is_file() or path.name not in _BROWSER_BINARIES:
            continue
        try:
            out = subprocess.run(
                ["ldd", str(path)], capture_output=True, text=True, timeout=30
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        missing = out.count("not found")
        if missing:
            broken.append((path.name, missing))
    return broken


def ensure(*, repair: bool = True) -> None:
    """No-op when the engines are healthy; repair (or explain) when they are not."""
    global _CHECKED
    if _CHECKED:
        return

    cache = _cache_dir()
    if not cache.is_dir():
        _CHECKED = True
        return  # no engines installed here -- not this guard's problem

    broken = _broken_engines(cache)
    if not broken:
        _CHECKED = True
        return

    listed = ", ".join(f"{name} ({n} missing)" for name, n in broken)
    cmd = os.environ.get("BROWSER_ENSURE_CMD", "").strip()

    if repair and cmd:
        print(f"[browser-guard] system libs missing: {listed}", file=sys.stderr)
        print(f"[browser-guard] repairing via BROWSER_ENSURE_CMD...", file=sys.stderr)
        subprocess.run(cmd, shell=True, check=False)
        # Re-verify: never announce "repaired" on the strength of an exit code.
        if not _broken_engines(cache):
            print("[browser-guard] repaired.", file=sys.stderr)
            _CHECKED = True
            return
        print("[browser-guard] STILL broken after repair.", file=sys.stderr)

    raise SystemExit(
        f"[browser-guard] Playwright system libraries are missing: {listed}\n"
        "  The browser binaries survive a container recreate, their .so files do not.\n"
        "  Install them with:  playwright install-deps chromium webkit\n"
        "  (or set BROWSER_ENSURE_CMD to a command that does it, and re-run)"
    )


if __name__ == "__main__":
    ensure(repair="--check-only" not in sys.argv)
    print("[browser-guard] OK")
