#!/usr/bin/env python3
"""Run an argv command in a sibling repo (tether verification helper).

Tether commands are executed with ``shell=False`` and no ``cd`` support, so a
literal ``cd ../QED && ...`` cannot be a ladder entry. This helper gives the
same effect while keeping every argument separate::

    python3 tools/run_in_repo.py ../QED python3 -m pytest test_pipeline.py -v

Exit status is the child's exit status.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cwd", help="working directory (e.g. ../QED)")
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="command and arguments")
    args = ap.parse_args(argv)
    command = list(args.command)
    if not command:
        print("no command given", file=sys.stderr)
        return 2
    cwd = Path(args.cwd)
    if not cwd.is_dir():
        print(f"working directory not found: {cwd}", file=sys.stderr)
        return 2
    return subprocess.call(command, cwd=str(cwd))


if __name__ == "__main__":
    raise SystemExit(main())
