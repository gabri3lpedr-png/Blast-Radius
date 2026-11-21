"""Turn the plugin's raw output into maps/coverage_map.json.gz (see the CONTRACT
in docs/GUIDE_A_MapMaker.md). Runs inside the mapgen image (needs coverage).

Usage: python export_map.py <raw dir with .coverage + blast_extra.json> <out .json.gz>
"""
import gzip
import json
import os
import sys
import time

from coverage import CoverageData

ROOT = "/opt/sqlglot/"
COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
MAX_BYTES = 12 * 1024 * 1024


def main(raw, out_path):
    t0 = time.time()
    extra = json.load(open(os.path.join(raw, "blast_extra.json")))
    tests = extra["tests"]
    index = {t: i for i, t in enumerate(tests)}

    sets, set_ids = [], {}

    def set_of(node_ids):
        key = tuple(sorted({index[n] for n in node_ids if n in index}))
        if key not in set_ids:
            set_ids[key] = len(sets)
            sets.append(list(key))
        return set_ids[key]

    data = CoverageData(basename=os.path.join(raw, ".coverage"))
    data.read()
    lines, import_lines = {}, {}
    for path in sorted(data.measured_files()):
        if not path.startswith(ROOT):
            continue
        rel = path[len(ROOT):]
        by_line = data.contexts_by_lineno(path)
        file_lines, file_imports = {}, []
        for lineno in sorted(by_line):
            ctxs = by_line[lineno]
            if "" in ctxs:
                file_imports.append(lineno)
            in_tests = [c for c in ctxs if c]
            if in_tests:
                file_lines[str(lineno)] = set_of(in_tests)
        if file_lines:
            lines[rel] = file_lines
        if file_imports:
            import_lines[rel] = file_imports

    files_read = {p: set_of(ns) for p, ns in extra["files_read"].items()}
    class_users = {k: set_of(ns) for k, ns in extra["class_users"].items()}

    meta = dict(extra["meta"])
    meta.update({
        "tests": len(tests),
        "sets": len(sets),
        "files_with_lines": len(lines),
        "lines": sum(len(v) for v in lines.values()),
        "import_lines": sum(len(v) for v in import_lines.values()),
        "files_read": len(files_read),
        "class_users": len(class_users),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    doc = {
        "version": 1,
        "sqlglot_commit": COMMIT,
        "tests": tests,
        "sets": sets,
        "lines": lines,
        "import_lines": import_lines,
        "files_read": files_read,
        "class_users": class_users,
        "failed_at_baseline": sorted(extra["failed"]),
        "meta": meta,
    }
    blob = gzip.compress(json.dumps(doc, separators=(",", ":")).encode(), 9, mtime=0)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(blob)
    os.chmod(out_path, 0o644)
    print(f"wrote {out_path}: {len(blob) / 1e6:.2f} MB gzipped, {len(tests)} tests, "
          f"{len(sets)} sets, {meta['lines']} lines, {meta['import_lines']} import lines, "
          f"{len(files_read)} files_read, {len(class_users)} class_users "
          f"({time.time() - t0:.1f} s)")
    if extra["failed"]:
        print(f"WARNING failed at baseline: {sorted(extra['failed'])}")
    if len(blob) > MAX_BYTES:
        sys.exit(f"MAP TOO BIG: {len(blob)} bytes > 12 MB. Ask before changing the contract.")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
