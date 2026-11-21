"""pytest plugin for label.py (DEV-ONLY): records which tests failed.

EVERY pytest process writes its own file, $LABEL_OUT.<worker>.json (worker =
"main" without xdist or for the xdist controller, else gw0, gw1, ...).
label.py merges them. Recording in the workers matters: with pytest-xdist,
subtest failures (SUBFAILED) are not passed on to the controller's hooks, so a
controller-only recorder labels a broken mutant as harmless.

A test counts as failing when any report for it failed (setup, call, teardown,
any subtest), or when it never reported ok (errors, timeouts, crashed workers,
unimportable files).
"""
import json
import os

_failed, _ok = set(), set()


def _worker(config):
    return getattr(config, "workerinput", {}).get("workerid", "main")


def pytest_configure(config):
    global _name
    _name = _worker(config)


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)
    elif report.when == "call" or report.skipped:
        _ok.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    with open(f"{os.environ['LABEL_OUT']}.{_name}.json", "w") as f:
        json.dump({"failed": sorted(_failed), "ok": sorted(_ok), "exitstatus": int(exitstatus)}, f)
