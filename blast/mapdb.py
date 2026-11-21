"""The coverage map (contract: docs/GUIDE_B_Selector.md section 2), loaded once, lazily.

maps/coverage_map.json.gz is used if present, else maps/stub_map.json.gz (same keys, empty).
Test ids are converted to numbers with tests.json at runtime: ids missing from tests.json are
ignored; tests.json tests missing from the map are UNMAPPED. Set indexes are converted to
number sets on first use and cached.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

MAPS_DIR = Path(os.environ.get("MAPS_DIR", Path(__file__).resolve().parent.parent / "maps"))
EMPTY: frozenset[int] = frozenset()


class MapDB:
    def __init__(self, raw: dict, number_by_id: dict[str, int], name: str = "?"):
        self.name = name
        ids = raw.get("tests") or []
        # map test index -> tests.json number (None when tests.json does not know the id)
        self._num = [number_by_id.get(t) for t in ids]
        self._raw_sets = raw.get("sets") or []
        self._sets: dict[int, frozenset[int]] = {}
        self._lines_raw: dict[str, dict[str, int]] = raw.get("lines") or {}
        self._lines: dict[str, dict[int, int]] = {}
        self._import_raw: dict[str, list[int]] = raw.get("import_lines") or {}
        self._import: dict[str, frozenset[int]] = {}
        self.files_read: dict[str, int] = raw.get("files_read") or {}
        self.class_users: dict[str, int] = raw.get("class_users") or {}
        mapped = {n for n in self._num if n is not None}
        self.unmapped: frozenset[int] = frozenset(set(number_by_id.values()) - mapped)
        base: set[int] = set()
        for t in raw.get("failed_at_baseline") or []:
            n = self._num[t] if isinstance(t, int) and 0 <= t < len(self._num) else number_by_id.get(t)
            if n is not None:
                base.add(n)
        self.failed_at_baseline: frozenset[int] = frozenset(base)

    @property
    def empty(self) -> bool:
        return not self._lines_raw

    def tests_of_set(self, idx: int | None) -> frozenset[int]:
        if idx is None:
            return EMPTY
        s = self._sets.get(idx)
        if s is None:
            raw = self._raw_sets[idx] if 0 <= idx < len(self._raw_sets) else []
            s = frozenset(n for n in (self._num[i] for i in raw if 0 <= i < len(self._num)) if n is not None)
            self._sets[idx] = s
        return s

    def _file_lines(self, path: str) -> dict[int, int]:
        d = self._lines.get(path)
        if d is None:
            d = {int(k): v for k, v in (self._lines_raw.get(path) or {}).items()}
            self._lines[path] = d
        return d

    def has_file(self, path: str) -> bool:
        """The map measured this file. A module whose code all runs at import (tsql.py: only
        class bodies) has import_lines but no per-test "lines"; it is mapped all the same."""
        return path in self._lines_raw or path in self._import_raw

    def line_set_ids(self, path: str, lines) -> set[int]:
        """The map set indexes of the tests that ran any of `lines` (one per distinct line set)."""
        d = self._file_lines(path)
        return {d[n] for n in lines if n in d}

    def file_lines(self, path: str) -> list[int]:
        """Every line of `path` the map credits to some test."""
        return sorted(self._file_lines(path))

    def class_keys_of_module(self, module: str) -> list[str]:
        prefix = module + ":"
        return [k for k in self.class_users if k.startswith(prefix)]

    def line_tests(self, path: str, lines) -> frozenset[int] | None:
        """Union of the tests that ran any of `lines` during a test; None if none ran any."""
        d = self._file_lines(path)
        idxs = {d[n] for n in lines if n in d}
        if not idxs:
            return None
        out: set[int] = set()
        for i in idxs:
            out |= self.tests_of_set(i)
        return frozenset(out)

    def import_lines(self, path: str) -> frozenset[int]:
        s = self._import.get(path)
        if s is None:
            s = frozenset(self._import_raw.get(path) or [])
            self._import[path] = s
        return s

    def file_read_set_id(self, path: str) -> int | None:
        return self.files_read.get(path)

    def file_read_tests(self, path: str) -> frozenset[int] | None:
        idx = self.files_read.get(path)
        return None if idx is None else self.tests_of_set(idx)

    def class_user_tests(self, key: str) -> frozenset[int] | None:
        idx = self.class_users.get(key)
        return None if idx is None else self.tests_of_set(idx)


def load(number_by_id: dict[str, int], maps_dir: Path = MAPS_DIR) -> MapDB:
    for name in ("coverage_map.json.gz", "stub_map.json.gz"):
        p = maps_dir / name
        if p.exists():
            with gzip.open(p, "rt", encoding="utf-8") as f:
                return MapDB(json.load(f), number_by_id, name)
    return MapDB({}, number_by_id, "none")
