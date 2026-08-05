"""An intermediate hop, so the daemon has no living parent.

Detaching from the console and breaking away from the job object is not enough.
Terminals, IDEs and agent harnesses clean up after a command with `taskkill /T` (or
the POSIX equivalent), which walks **parent → child** links — and breaking away from a
job does not change who your parent is. A daemon spawned directly from `council up`
therefore dies whenever the shell that ran it is cleaned up, however detached it is.

So `council up` starts this instead. It starts the real daemon and exits immediately,
which leaves the daemon parented to a process that no longer exists: on nobody's tree,
and safe to outlive the session that asked for it. The classic double-fork, spelled
out for a platform that has no fork.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

#: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
WINDOWS_FLAGS = 0x00000008 | 0x00000200 | 0x01000000
WINDOWS_FLAGS_NO_BREAKAWAY = 0x00000008 | 0x00000200


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="council.server.launcher")
    parser.add_argument("--log", required=True, help="file the daemon's output appends to")
    parser.add_argument("rest", nargs=argparse.REMAINDER, help="-- then the daemon's argv")
    args = parser.parse_args(argv)

    command = args.rest[1:] if args.rest and args.rest[0] == "--" else args.rest
    if not command:
        print("launcher: nothing to start", file=sys.stderr)
        return 2

    log = open(args.log, "a", encoding="utf-8")
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        try:
            subprocess.Popen(command, creationflags=WINDOWS_FLAGS, **kwargs)
        except OSError:
            # The job forbids breakaway; detaching from the console is still worth it.
            subprocess.Popen(command, creationflags=WINDOWS_FLAGS_NO_BREAKAWAY, **kwargs)
    else:
        subprocess.Popen(command, start_new_session=True, **kwargs)
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
