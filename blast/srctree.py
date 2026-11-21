"""Extract the pinned sqlglot source tarball (GitHub archive) into a directory.

Used by the Dockerfile at build time (-> /opt/sqlglot) and by tools/eval_sample.py for local
runs (-> .cache/sqlglot). The archive's single top-level folder is stripped, so
<dest>/sqlglot/parser.py is the pinned sqlglot/parser.py.

Usage: python -m blast.srctree <tarball> <dest>
"""

from __future__ import annotations

import sys
import tarfile
from pathlib import Path

SQLGLOT_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
TARBALL_URL = f"https://github.com/tobymao/sqlglot/archive/{SQLGLOT_COMMIT}.tar.gz"


def extract(tarball: str | Path, dest: str | Path) -> None:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as tf:
        members = []
        for m in tf.getmembers():
            _, sep, rest = m.name.partition("/")
            if not sep or not rest:
                continue  # the top-level folder itself
            m.name = rest
            members.append(m)
        tf.extractall(dest, members=members, filter="data")


if __name__ == "__main__":
    extract(sys.argv[1], sys.argv[2])
