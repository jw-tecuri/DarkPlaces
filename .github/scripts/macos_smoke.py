#!/usr/bin/env python3
"""Run a DarkPlaces binary on a pty and require a headless missing-data startup.

GitHub Actions has no usable terminal. DarkPlaces marks stdout non-blocking and,
on macOS, the SDL client pulls in Cocoa, so a plain pipe often stays empty.
A pty matches a normal Terminal launch.
"""

from __future__ import annotations

import os
import pty
import select
import signal
import sys
import time


def run(argv: list[str], seconds: float) -> tuple[str, int | None, bool]:
    output = bytearray()
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(argv[0], argv)
        os._exit(127)
    status: int | None = None
    timed_out = False
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            readable, _, _ = select.select([fd], [], [], 0.2)
            if readable:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            waited, code = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = code
                break
        else:
            timed_out = True
            os.kill(pid, signal.SIGTERM)
            term_deadline = time.time() + 5
            while time.time() < term_deadline:
                readable, _, _ = select.select([fd], [], [], 0.2)
                if readable:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                waited, code = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    status = code
                    break
            else:
                os.kill(pid, signal.SIGKILL)
                _, status = os.waitpid(pid, 0)
    finally:
        os.close(fd)
    text = output.decode("utf-8", "replace")
    return text, status, timed_out


def main() -> None:
    args = sys.argv[1:]
    must_exit = False
    if args and args[0] == "--must-exit":
        must_exit = True
        args = args[1:]
    if len(args) < 2:
        print("usage: macos_smoke.py [--must-exit] <seconds> <binary> [args...]", file=sys.stderr)
        sys.exit(2)
    seconds = float(args[0])
    argv = args[1:]
    empty = os.environ["SMOKE_BASEDIR"]
    argv.extend(["-dedicated", "-nocrashdialog", "-nohome", "-stderr", "-basedir", empty])
    print(f"smoke: {argv}", flush=True)
    text, status, timed_out = run(argv, seconds)
    print("----- output -----")
    print(text)
    print("----- end -----")
    print(f"wait status: {status} timed_out: {timed_out}")
    lowered = text.lower()
    failed = []
    if timed_out and must_exit:
        failed.append("process was still running when the timeout fired")
    if not text.strip():
        failed.append("no console output")
    if "failed to load sdl3" in lowered or "libsdl3" in lowered:
        failed.append("SDL3 load failure")
    if "library not loaded" in lowered or "image not found" in lowered or "dlopen(" in lowered:
        failed.append("dlopen/dyld error")
    if "engine crash" in lowered:
        failed.append("engine crash")
    if "macos dedicated" not in lowered:
        failed.append("missing version banner")
    if not must_exit:
        if "empty or not found" not in lowered:
            failed.append("did not report missing id1")
        if "no map file" not in lowered:
            failed.append("did not report missing map")
        if 'loaded library "libcurl' not in lowered:
            failed.append("did not dlopen libcurl")
    if failed:
        print("smoke test failed: " + "; ".join(failed), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
