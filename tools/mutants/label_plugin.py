"""pytest plugin for label.py (DEV-ONLY): records which tests failed.

Writes $LABEL_OUT as {"failed": [...], "ok": [...], "exitstatus": n} from the
xdist controller (or the only process without xdist). label.py counts a test
as failing when it failed (any phase, any subtest) or never reported ok: that
covers SUBFAILED, errors, timeouts, crashed workers and unimportable files.
"""
import json
import os

_failed, _ok = set(), set()


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)
    elif report.when == "call" or report.skipped:
        _ok.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput"):
        return
    with open(os.environ["LABEL_OUT"], "w") as f:
        json.dump({"failed": sorted(_failed), "ok": sorted(_ok), "exitstatus": int(exitstatus)}, f)
