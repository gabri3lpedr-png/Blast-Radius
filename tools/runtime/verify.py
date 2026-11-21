"""DEV-ONLY: run a commit's selected tests on a /tmp overlay of /opt/sqlglot and time it.

Inside tools/runtime/Dockerfile.verify, under the scorer's docker flags:
  /data/commits/<id>.patch, /data/tests.json  as in the real run
  /sel/output.jsonl                            our selector's output for those commits
  argv[1:]                                     commit ids to check
For each commit: overlay = real dirs along the patched paths, everything else symlinked (incl.
__pycache__, byte-compiled at build); then
  1. `pytest --collect-only` on the selected ids  -> startup + collection CPU
  2. `pytest` on the selected ids                 -> total CPU, failing ids (label_plugin)
CPU is user+sys of the child processes (RUSAGE_CHILDREN), wall too. One JSON line per commit on
stdout.
"""
import json
import os
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")
from blast import diff  # noqa: E402
from blast.apply import apply  # noqa: E402

SRC = Path("/opt/sqlglot")
OV = Path("/tmp/ov")


def overlay(patched: dict[str, str]) -> None:
    if OV.exists():
        shutil.rmtree(OV)
    real = {"."}
    for p in patched:
        parts = p.split("/")
        for i in range(1, len(parts)):
            real.add("/".join(parts[:i]))

    def build(rel: str) -> None:
        dst = OV if rel == "." else OV / rel
        dst.mkdir(parents=True, exist_ok=True)
        for entry in os.listdir(SRC if rel == "." else SRC / rel):
            r = entry if rel == "." else f"{rel}/{entry}"
            if r in patched:
                (OV / r).write_text(patched[r], "utf-8")
            elif r in real:
                build(r)
            else:
                os.symlink(SRC / r, OV / r)
        for r, text in patched.items():  # new files
            if r.rpartition("/")[0] == ("" if rel == "." else rel) and not (OV / r).exists():
                (OV / r).write_text(text, "utf-8")

    build(".")


def run(args, out_tag):
    env = dict(os.environ, PYTHONPATH=f"{OV}:/verify", LABEL_OUT=f"/tmp/{out_tag}", HOME="/tmp")
    if os.environ.get("VERIFY_CPU_COUNT"):  # os.cpu_count() sizes TestOptimizer's process pool
        env["PYTHON_CPU_COUNT"] = os.environ["VERIFY_CPU_COUNT"]
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.time()
    p = subprocess.run([sys.executable, "-m", "pytest", *args, "-p", "label_plugin", "-p", "no:cacheprovider",
                        "-q", "--timeout", "300", "-o", "addopts="],
                       cwd=OV, env=env, capture_output=True, text=True)
    wall = time.time() - t0
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
    res = {}
    f = Path(f"/tmp/{out_tag}.main.json")
    if f.exists():
        res = json.loads(f.read_text())
        f.unlink()
    return cpu, wall, res, p


def main():
    tests = json.loads(Path("/data/tests.json").read_text())["tests"]
    id_of = {t["number"]: t["id"] for t in tests}
    sel = {}
    for line in Path("/sel/output.jsonl").read_text().splitlines():
        r = json.loads(line)
        sel[r["commit"]] = sorted(n for w in r["workers"] for n in w)
    for c in sys.argv[1:]:
        text = Path(f"/data/commits/{c}.patch").read_bytes().decode("utf-8", "replace")
        patched = {}
        for fp in diff.parse(text):
            orig = (SRC / fp.path).read_text("utf-8") if not fp.is_new else ""
            patched[fp.path], _ = apply(fp, orig)
        t0 = time.time()
        overlay(patched)
        t_ov = time.time() - t0
        ids = [id_of[n] for n in sel[c]]
        c_cpu, c_wall, _, _ = run(["--collect-only", *ids], f"{c}.co")
        cpu, wall, res, p = run(ids, c)
        failed = set(res.get("failed", []))
        never = sorted(set(ids) - set(res.get("ok", [])) - failed)
        print(json.dumps({"commit": c, "n": len(ids), "overlay_s": round(t_ov, 3),
                          "collect_cpu": round(c_cpu, 3), "collect_wall": round(c_wall, 3),
                          "cpu": round(cpu, 3), "wall": round(wall, 3),
                          "failed": sorted(failed | set(never)), "never_reported": never,
                          "exitstatus": res.get("exitstatus"), "tail": p.stdout[-300:] if not res else ""}),
              flush=True)


if __name__ == "__main__":
    main()
