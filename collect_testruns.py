#!/usr/bin/env python3
"""Per-test outcome collection across many CI runs, for flake detection.

`flake_analyzer.py` classifies one failed job in isolation. Flakiness is a
property of a test across runs, so it needs a different collection shape: every
test outcome, pass and fail, keyed by test and joined on commit and branch.

Podman's system tests are BATS emitting TAP, which prints `ok N <name>` for
passes as well as `not ok N <name>` for failures. One failed job's log therefore
carries the full pass/fail vector for every test in that job, which is what
makes this affordable: only failed runs are walked, and passing tests still get
counted.

Logs are streamed and discarded, never cached. The 850 MB the analyzer's cache
reached over 40 runs does not extrapolate to the several hundred needed here.

Writes one JSON-lines record per (job, test) observation to --out.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

API = "https://api.github.com"

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\[\d{1,2}(;\d{1,2})*m")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?")

# TAP, as emitted by BATS:  `ok 42 [610] podman foo in 123ms`
# The trailing `in NNNms` is per-run timing and must be stripped, or the same
# test fragments into one id per duration (contamination C5).
TAP = re.compile(r"^(not )?ok (\d+) (.*?)$")
TAP_DURATION = re.compile(r"\s+in \d+ms\s*$")
TAP_DIRECTIVE = re.compile(r"\s*#\s*(skip|todo)\b.*$", re.IGNORECASE)

# Ginkgo:  `[FAIL] Podman run [It] some spec name`
GINKGO_FAIL = re.compile(
    r"^\s*\[FAIL\]\s+(\S.*?\[(?:It|DeferCleanup|BeforeEach|AfterEach|BeforeAll|"
    r"AfterAll|JustBeforeEach)[^\]]*\].*\S)\s*$"
)
# Ginkgo prints a summary of passed specs only in verbose mode, so Ginkgo jobs
# contribute failures but not reliable passes. Recorded, not silently mixed.


class GitHub:
    def __init__(self, token: str):
        self.token = token
        self.lock = Lock()
        self.calls = 0

    def _req(self, url: str, raw: bool):
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "podman-flake-collector",
            },
        )

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(*([_NoRedirect] if raw else []))
        for attempt in range(4):
            try:
                with opener.open(req, timeout=120) as r:
                    return r.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                if raw and e.code in (301, 302, 303, 307, 308):
                    loc = e.headers.get("Location")
                    if not loc:
                        return None
                    try:
                        clean = urllib.request.Request(
                            loc, headers={"User-Agent": "podman-flake-collector"}
                        )
                        with urllib.request.urlopen(clean, timeout=300) as r2:
                            return r2.read().decode("utf-8", errors="replace")
                    except Exception:
                        time.sleep(2 ** attempt)
                        continue
                if e.code in (404, 410):
                    return None          # 410 = purged by retention (C4)
                if e.code in (403, 429):
                    reset = e.headers.get("X-RateLimit-Reset")
                    wait = 60
                    if reset:
                        wait = max(5, int(reset) - int(time.time()) + 5)
                    time.sleep(min(wait, 300))
                    continue
                if e.code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                return None
            except (urllib.error.URLError, TimeoutError, ConnectionError,
                    http.client.HTTPException, OSError):
                # RemoteDisconnected subclasses ConnectionResetError and
                # BadStatusLine, and was not caught by the original clause;
                # it killed worker threads mid-collection.
                time.sleep(2 ** attempt)
        return None

    def get(self, path: str, raw: bool = False):
        url = path if path.startswith("http") else f"{API}/{path.lstrip('/')}"
        with self.lock:
            self.calls += 1
        text = self._req(url, raw)
        if text is None:
            return None
        return text if raw else json.loads(text)


def normalise_tap(name: str) -> str:
    """Strip per-run noise so one test is one id (contamination C5)."""
    n = TAP_DIRECTIVE.sub("", name)
    n = TAP_DURATION.sub("", n)
    return " ".join(n.split()).strip()


def parse_log(log: str) -> tuple[list[tuple[str, bool]], str]:
    """Return ([(test_id, passed)], harness) for one job log."""
    out: list[tuple[str, bool]] = []
    harness = "unknown"
    for raw in log.splitlines():
        ln = ANSI.sub("", TIMESTAMP.sub("", raw))
        m = TAP.match(ln)
        if m:
            harness = "bats"
            failed = bool(m.group(1))
            name = normalise_tap(m.group(3))
            if name:
                out.append((name, not failed))
            continue
        g = GINKGO_FAIL.match(ln)
        if g:
            if harness == "unknown":
                harness = "ginkgo"
            out.append((" ".join(g.group(1).split()), False))
    return out, harness


def collect(gh: GitHub, repo: str, workflow: str, limit: int,
            status: str, workers: int, out_path: Path, verbose: bool,
            job_filter: str) -> dict:
    runs: list[dict] = []
    page = 1
    while len(runs) < limit and page <= 12:
        q = f"per_page=100&page={page}" + (f"&status={status}" if status else "")
        data = gh.get(f"repos/{repo}/actions/workflows/{workflow}/runs?{q}")
        if not data or not data.get("workflow_runs"):
            break
        runs.extend(data["workflow_runs"])
        page += 1
    runs = runs[:limit]
    print(f"[1/3] {len(runs)} runs (status={status or 'any'})", file=sys.stderr)

    jobs: list[dict] = []
    keep = re.compile(job_filter) if job_filter else None

    def jobs_for(run: dict) -> list[dict]:
        got = []
        p = 1
        while p <= 5:
            d = gh.get(f"repos/{repo}/actions/runs/{run['id']}/jobs"
                       f"?per_page=100&page={p}")
            if not d or not d.get("jobs"):
                break
            for j in d["jobs"]:
                # Only jobs that actually ran tests carry TAP. Fetching all ~80
                # jobs per run is not tractable; the BATS `sys` jobs are the
                # ones that emit a full pass/fail vector.
                if keep and not keep.search(j.get("name", "")):
                    continue
                got.append({
                    "run_id": run["id"],
                    "run_attempt": run.get("run_attempt", 1),
                    "branch": run.get("head_branch") or "?",
                    "sha": (run.get("head_sha") or "")[:12],
                    "event": run.get("event") or "?",
                    "created": (run.get("created_at") or "")[:19],
                    "job_id": j["id"],
                    "job_name": j.get("name", "?"),
                    "conclusion": j.get("conclusion"),
                })
            if len(d["jobs"]) < 100:
                break
            p += 1
        return got

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for got in ex.map(jobs_for, runs):
            jobs.extend(got)
    print(f"[2/3] {len(jobs)} jobs", file=sys.stderr)

    stats = {"jobs_seen": len(jobs), "logs_ok": 0, "logs_410": 0,
             "obs": 0, "bats_jobs": 0, "ginkgo_jobs": 0}
    lock = Lock()
    fh = out_path.open("w", encoding="utf-8")

    def work(j: dict) -> None:
        log = gh.get(f"repos/{repo}/actions/jobs/{j['job_id']}/logs", raw=True)
        if not log:
            with lock:
                stats["logs_410"] += 1
            return
        obs, harness = parse_log(log)
        del log                                     # stream and discard
        if not obs:
            with lock:
                stats["logs_ok"] += 1
            return
        rows = [
            json.dumps({**{k: j[k] for k in
                           ("run_id", "run_attempt", "branch", "sha", "event",
                            "created", "job_id", "job_name", "conclusion")},
                        "harness": harness, "test": t, "passed": p})
            for t, p in obs
        ]
        with lock:
            stats["logs_ok"] += 1
            stats["obs"] += len(rows)
            if harness == "bats":
                stats["bats_jobs"] += 1
            elif harness == "ginkgo":
                stats["ginkgo_jobs"] += 1
            fh.write("\n".join(rows) + "\n")
            fh.flush()
            if verbose and stats["logs_ok"] % 25 == 0:
                print(f"      {stats['logs_ok']} logs, {stats['obs']} obs",
                      file=sys.stderr)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, jobs))
    fh.close()
    print(f"[3/3] {stats['obs']} observations -> {out_path}", file=sys.stderr)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default="podman-container-tools/podman")
    ap.add_argument("--workflow", default="ci.yml")
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--status", default="failure",
                    help="'failure' or '' for all runs (control sample, C7)")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--out", default="testruns.jsonl")
    ap.add_argument("--job-filter", default=r"^sys ",
                    help="regex on job name; '' for all jobs")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    try:
        tok = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, check=True).stdout.strip()
    except Exception:
        tok = os.environ.get("GITHUB_TOKEN", "")
    if not tok:
        print("no token", file=sys.stderr)
        return 1

    gh = GitHub(tok)
    stats = collect(gh, a.repo, a.workflow, a.limit, a.status,
                    a.workers, Path(a.out), a.verbose, a.job_filter)
    stats["api_calls"] = gh.calls
    print(json.dumps(stats, indent=1), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
