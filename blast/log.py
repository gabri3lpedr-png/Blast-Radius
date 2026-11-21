"""Logging helper: everything goes to stderr, never stdout (stdout is the answer channel)."""

import sys


def log(*args) -> None:
    print(*args, file=sys.stderr, flush=True)
