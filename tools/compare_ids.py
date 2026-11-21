#!/usr/bin/env python3
"""Compare the test ids from a JUnit baseline run with sample/tests.json.

Usage: python3 tools/compare_ids.py [mapgen_out/baseline.xml] [sample/tests.json]

Reports how many tests.json ids the run found, which are missing, which are
extra, and every test that failed, errored or had a failing subtest.
"""
import json
import sys
import xml.etree.ElementTree as ET


def node_id(case, known_files):
    """JUnit classname 'tests.dialects.test_x.TestX' + name -> pytest node id."""
    classname = case.get("classname", "")
    name = case.get("name", "")
    parts = classname.split(".")
    # Longest prefix of the dotted classname that is a known test file is the module.
    for i in range(len(parts), 0, -1):
        path = "/".join(parts[:i]) + ".py"
        if path in known_files or i == 1:
            rest = parts[i:]
            break
    if path not in known_files:
        # Fallback: module ends at the last lowercase part before a Test* class.
        i = next((k for k, p in enumerate(parts) if p[:1].isupper()), len(parts))
        path, rest = "/".join(parts[:i]) + ".py", parts[i:]
    return "::".join([path, *rest, name])


def main():
    xml_path = sys.argv[1] if len(sys.argv) > 1 else "mapgen_out/baseline.xml"
    tests_path = sys.argv[2] if len(sys.argv) > 2 else "sample/tests.json"

    tj = json.load(open(tests_path))
    expected = {t["id"] for t in tj["tests"]}
    skipped_expected = set(tj.get("skipped", []))
    known_files = {t["file"] for t in tj["tests"]} | {i.split("::")[0] for i in skipped_expected}

    found, failed, skipped_run = set(), {}, set()
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        nid = node_id(case, known_files)
        found.add(nid)
        for tag in ("failure", "error"):
            el = case.find(tag)
            if el is not None:
                failed.setdefault(nid, f"{tag}: {(el.get('message') or '')[:120]}")
        if case.find("skipped") is not None:
            skipped_run.add(nid)

    ran = found - skipped_run
    print(f"tests.json ids:          {len(expected)}")
    print(f"found in run (not skip): {len(expected & ran)}")
    print(f"missing from run:        {len(expected - ran)}")
    for i in sorted(expected - ran)[:25]:
        print(f"   - {i}{'  (skipped in run)' if i in skipped_run else ''}")
    extra = ran - expected - skipped_expected
    print(f"extra (ran, not listed): {len(extra)}")
    for i in sorted(extra)[:25]:
        print(f"   + {i}")
    print(f"tests.json 'skipped':    {len(skipped_expected)} "
          f"(also skipped in our run: {len(skipped_expected & skipped_run)})")
    print(f"failed/errored at baseline: {len(failed)}")
    for i, why in sorted(failed.items()):
        print(f"   ! {i}  -- {why}")
    return 0 if not (expected - ran) and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
