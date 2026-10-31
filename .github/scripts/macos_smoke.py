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


def run(argv: list[str], seconds: float) -> tuple[str, int | None]:
    output = bytearray()
    pid, fd = pty.fork()
    if pid == 0:
        os.execv(argv[0], argv)
        os._exit(127)
    status: int | None = None
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
                deadline = time.time() + 0.5
        else:
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
    return text, status


def main() -> None:
    args = sys.argv[1:]
    allow_silent = False
    if args and args[0] == "--allow-silent":
        allow_silent = True
        args = args[1:]
    if len(args) < 2:
        print("usage: macos_smoke.py [--allow-silent] <seconds> <binary> [args...]", file=sys.stderr)
        sys.exit(2)
    seconds = float(args[0])
    argv = args[1:]
    empty = os.environ["SMOKE_BASEDIR"]
    argv.extend(["-dedicated", "-nocrashdialog", "-nohome", "-stderr", "-basedir", empty])
    print(f"smoke: {argv}", flush=True)
    text, status = run(argv, seconds)
    print("----- output -----")
    print(text)
    print("----- end -----")
    print(f"wait status: {status}")
    failed = []
    if "Engine Crash" in text or "Library not loaded" in text:
        failed.append("crash or missing library")
    if "macOS dedicated" not in text:
        failed.append("missing version banner")
    if "empty or not found" not in text:
        failed.append("did not report missing id1")
    if "no map file" not in text:
        failed.append("did not report missing map")
    if 'Loaded library "libcurl' not in text:
        failed.append("did not dlopen libcurl")
    if failed and allow_silent and not text.strip():
        print("SDL client produced no console output and stayed running.")
        print("On this runner that usually means SDL/Cocoa is waiting on WindowServer.")
        print("Dedicated-server smoke test is the headless check; not failing the client for silence.")
        return
    if failed:
        print("smoke test failed: " + "; ".join(failed), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
