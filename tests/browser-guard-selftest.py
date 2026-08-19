#!/usr/bin/env python3
"""Self-test for `browser_guard.ensure()`.

The case that matters is the POSITIVE CONTROL: with a missing `.so`, the guard
must stop the run. A self-test that only exercised the healthy path would stay
green against a dead predicate -- which is precisely the failure mode the guard
exists to prevent. `ldd` is shadowed through PATH to stage both states.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
fails = []


def run(*, ldd_out, cache, env_extra=None, args=()):
    tmp = Path(cache)
    binpath = tmp / "stub"
    binpath.mkdir(parents=True, exist_ok=True)
    (binpath / "ldd").write_text(f'#!/bin/sh\n{ldd_out}\n')
    (binpath / "ldd").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{binpath}:{env['PATH']}"
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(tmp / "cache")
    env.pop("BROWSER_ENSURE_CMD", None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(HERE / "browser_guard.py"), *args],
        capture_output=True, text=True, env=env, cwd=str(HERE),
    )


def check(label, cond):
    print(("  OK   " if cond else "  KO   ") + label)
    if not cond:
        fails.append(label)


with tempfile.TemporaryDirectory() as td:
    eng = Path(td) / "cache" / "chromium-1" / "chrome-linux"
    eng.mkdir(parents=True)
    (eng / "chrome-headless-shell").touch()

    print("== A: a missing lib, no repair command (positive control) ==")
    r = run(ldd_out='echo "\tlibglib-2.0.so.0 => not found"', cache=td)
    check("exits non-zero", r.returncode != 0)
    check("names the missing count", "missing" in (r.stdout + r.stderr))
    check("tells what to run", "install-deps" in (r.stdout + r.stderr))

    print("== B: healthy engines ==")
    r = run(ldd_out='echo "\tlibc.so.6 => /lib/libc.so.6"', cache=td)
    check("exits 0", r.returncode == 0)
    check("says OK", "OK" in r.stdout)

    print("== C: repair command is honoured, then RE-VERIFIED ==")
    # The command rewrites the ldd stub to a healthy one -> guard must accept.
    fixcmd = f"printf '#!/bin/sh\\necho ok\\n' > {td}/stub/ldd; chmod +x {td}/stub/ldd"
    r = run(ldd_out='echo "\tlibglib-2.0.so.0 => not found"', cache=td,
            env_extra={"BROWSER_ENSURE_CMD": fixcmd})
    check("exits 0 after a real repair", r.returncode == 0)
    check("says repaired", "repaired" in r.stderr)

    print("== D: a repair command that FIXES NOTHING must still fail ==")
    r = run(ldd_out='echo "\tlibglib-2.0.so.0 => not found"', cache=td,
            env_extra={"BROWSER_ENSURE_CMD": "true"})
    check("exits non-zero", r.returncode != 0)
    check("says still broken", "STILL broken" in r.stderr)

    print("== E: no engines installed is not an error ==")
    r = run(ldd_out='echo "\tlibc.so.6 => /lib/libc.so.6"', cache=td + "/nope")
    check("exits 0", r.returncode == 0)

print()
if fails:
    print(f"FAILED: {len(fails)}")
    sys.exit(1)
print("ALL PINS HOLD")
