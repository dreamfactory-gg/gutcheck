"""Detached background jobs (review, explainer): a job dir under STATE_DIR/jobs with status.json and the outputs.

Jobs only read the repo. They never block the session; the caller records the job dir and reads it later.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from gutcheck_core import config

JOBS = config.STATE_DIR / "jobs"
SRC = Path(__file__).resolve().parent.parent


def slug(text, n=48):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if len(s) > n:
        s = s[:n + 1]
        s = s[:s.rindex("-")] if "-" in s[n // 2:] else s[:n]
    return s.strip("-") or "change"


def new(kind, label):
    JOBS.mkdir(parents=True, exist_ok=True)
    jid = f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{slug(label, 32)}"
    d = JOBS / jid
    d.mkdir(parents=True, exist_ok=True)
    return jid, d


def write_status(d, **kw):
    p = Path(d) / "status.json"
    cur = {}
    if p.exists():
        try:
            cur = json.loads(p.read_text())
        except ValueError:
            cur = {}
    cur.update(kw, updated=time.strftime("%Y-%m-%dT%H:%M:%S"))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=1))
    tmp.replace(p)
    return cur


def read_status(d):
    try:
        return json.loads((Path(d) / "status.json").read_text())
    except (OSError, ValueError):
        return {}


def spawn(d, argv):
    """Run `python -m tokenmaxxing.cli <argv>` detached (own session, no stdin), output to d/job.log."""
    env = dict(os.environ, PYTHONPATH=str(SRC) + (os.pathsep + os.environ["PYTHONPATH"]
                                                  if os.environ.get("PYTHONPATH") else ""))
    with open(Path(d) / "job.log", "ab") as log:
        p = subprocess.Popen([sys.executable, "-m", "tokenmaxxing.cli", *argv], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, env=env, start_new_session=True, cwd=str(d))
    write_status(d, state="running", pid=p.pid, started=time.strftime("%Y-%m-%dT%H:%M:%S"))
    return p.pid


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def listing(kind=None, limit=10):
    if not JOBS.exists():
        return []
    out = []
    for d in sorted(JOBS.iterdir(), reverse=True):
        if kind and f"-{kind}-" not in d.name:
            continue
        st = read_status(d)
        if st.get("state") == "running" and st.get("pid") and not alive(st["pid"]):
            st["state"] = "died"
        out.append((d, st))
        if len(out) >= limit:
            break
    return out
