"""SAFE_GROUPS: switch whole families of narrowing rules back to FULL (env, comma list).

  A  NOOP shortcuts: docstring-only edits, documentation files, mode-only changes
     (AST-equal comment/whitespace edits stay NOOP: that is proof, not a shortcut)
  B  import-time inference: class_users and the module-level name chain (rule b)
  C  line/def map selection for changed runtime code (rules a + c on def bodies)
  D  fixture selection: files_read and the fixture-name fallback (rule e)
  E  DEAD-code rule and empty lookups that contribute [] for a changed line or name

Empty (the default) keeps every rule on. Unknown letters are ignored (and logged).
"""

from __future__ import annotations

import os

KNOWN = frozenset("ABCDE")


def parse(value: str | None) -> frozenset[str]:
    return frozenset(g.strip().upper() for g in (value or "").split(",") if g.strip())


GROUPS: frozenset[str] = parse(os.environ.get("SAFE_GROUPS"))


def on(group: str) -> bool:
    return group in GROUPS


def describe() -> str:
    unknown = GROUPS - KNOWN
    s = ",".join(sorted(GROUPS & KNOWN)) or "none"
    return s + (f" (ignored unknown: {','.join(sorted(unknown))})" if unknown else "")
