#!/usr/bin/env python3
"""
CI failure categorization for GitHub Actions workflows.

Ingests failed workflow runs, resolves them to failed jobs, fetches the job
logs, and classifies each failure against a rule taxonomy. Rerun history is
used as ground truth: a job that fails on one attempt and passes on the latest
attempt of the same run is a confirmed flake.

Requires the gh CLI for auth (`gh auth login`) or GITHUB_TOKEN. No third-party
dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

API = "https://api.github.com"
CACHE = Path(__file__).parent / ".cache"


# Root-cause taxonomy. First match wins, so specific signatures are ordered
# before generic ones.

TAXONOMY: list[tuple[str, str, list[str]]] = [
    (
        "infra_runner",
        "Runner or host environment failure, not the code under test",
        [
            r"No space left on device",
            r"The runner has received a shutdown signal",
            r"lost communication with the server",
            r"The self-hosted runner .* lost communication",
            r"The operation was canceled",
            r"The hosted runner .* was unable to communicate",
            r"Out of memory|OOMKilled|oom-kill",
            r"cannot allocate memory",
            r"The job running on runner .* has exceeded the maximum execution time",
        ],
    ),
    (
        "github_infra",
        "GitHub's own infrastructure throttled or failed the job",
        [
            r"429 \(Too Many Requests\)",
            r"Failed to download action ",
            r"Failed to download archive .*codeload\.github\.com",
            r"Response status code does not indicate success: 4\d\d",
            r"Error: Response status code does not indicate success: 5\d\d",
        ],
    ),
    (
        "network_timeout",
        "Network unreachable, DNS failure, or transport timeout",
        [
            # HTTP 5xx from an asset download.
            r"curl: \(\d+\) The requested URL returned error: 5\d\d",
            r"50[234] Gateway Time-?out",
            r"The server didn't respond in time",
            r"Invoke-WebRequest.*(504|502|503|timed out)",
            r"dial tcp .*: i/o timeout",
            r"dial tcp .*: connect: connection refused",
            r"TLS handshake timeout",
            r"Temporary failure in name resolution",
            r"Could not resolve host",
            r"net/http: request canceled while waiting for connection",
            r"connection reset by peer",
            r"EOF occurred in violation of protocol",
            r"Failed to connect to .* port \d+",
            r"server misbehaving",
            r"curl: \(\d+\) (Recv failure|Operation timed out|Connection timed out)",
        ],
    ),
    (
        "vm_provisioning",
        "Test VM / hypervisor provisioning failure (WSL, Hyper-V, lima, applehv)",
        [
            r"aka\.ms/wslinstall",
            r"WslRegisterDistribution failed",
            r"The virtual machine could not be started",
            r"HCS_E_|Hyper-V.*not (installed|enabled)",
            r"limactl .*(failed|error)",
            r"failed to start .*(machine|vm)",
        ],
    ),
    (
        "registry_pull",
        "Container image registry unavailable, rate limited, or missing tag",
        [
            r"toomanyrequests",
            r"error pulling image",
            r"Error: initializing source docker://",
            r"manifest unknown",
            r"unauthorized: authentication required",
            r"(quay\.io|docker\.io|registry\.access\.redhat\.com).*(timeout|refused|error|500|502|503)",
            r"reading manifest .* in (quay\.io|docker\.io)",
        ],
    ),
    (
        "dependency_install",
        "Package manager or module download failure",
        [
            r"go: (downloading|module) .*(timeout|connection|unrecognized|reset)",
            r"(dnf|yum|apt-get|apk|brew) .*(Error|failed|Failed|Cannot)",
            r"Failed to download metadata for repo",
            r"Could not resolve dependencies",
            r"npm ERR!",
            r"Errors during downloading metadata",
        ],
    ),
    (
        "timing_race",
        "Timing-dependent failure: deadline, race detector, or wait loop",
        [
            r"context deadline exceeded",
            r"timed out waiting for",
            r"WARNING: DATA RACE",
            r"Timed out after \d",
            r"panic: test timed out",
            r"deadlock",
            r"still running after",
            r"Timeout .* exceeded while waiting",
        ],
    ),
    (
        "lint_static",
        "Linter or static analysis violation",
        [
            # Not a bare golangci-lint match: the tool prints a version banner
            # on every successful run.
            r"\(govet\)|\(staticcheck\)|\(revive\)|\(gofmt\)|\(errcheck\)|\(gosec\)",
            r"File is not `?gofmt`?-ed",
            r"is not properly formatted",
            r"golangci-lint.*\d+ issues?:",
            r"validate\.(sh|py).*failed",
        ],
    ),
    (
        "size_regression",
        "Binary size gate: build grew past the allowed delta",
        [
            r"grew by \d+ bytes; max allowed is",
            r"make-and-check-size",
            r"bloat_approved",
        ],
    ),
    (
        "test_assertion",
        "Test assertion failure, most likely a genuine defect",
        [
            r"\[FAILED\]",
            r"Expected\s+<.*>\s+to",
            r"--- FAIL: ",
            r"Summarizing \d+ Failure",
            r"assertion failed",
            r"Test Suite Failed",
            r"Exit code = '\d+' running '?\./bin/ginkgo",
            r"Ginkgo ran \d+ suite.* in .*FAIL",
            r"^not ok \d+",
        ],
    ),
    # Ordered last: a compile or packaging error is only the cause when nothing
    # more specific matched. `make: *** [target] Error N` is intentionally not
    # here; see GENERIC_ERRORS.
    (
        "build_artifact",
        "Build, packaging, or artifact upload failure",
        [
            r"No files were found with the provided path",
            r"##\[error\].*\.go:\d+:\d+:",
            r"undefined: ",
            r"cannot find package",
            r"build constraints exclude all Go files",
        ],
    ),
]

# Terminal markers that report that something failed without reporting what.
# Never used as evidence, and never used alone to anchor the extraction window.
GENERIC_ERRORS = re.compile(
    r"Process completed with exit code|"
    r"##\[error\]The (action|process) .* failed|"
    r"Error: Process completed|"
    # `make: *** [Makefile:705: ginkgo] Error 2` reports only that a target
    # failed. Podman runs its test suites through make targets (ginkgo,
    # localsystem, localmachine), so matching this as a build failure
    # mislabels test failures.
    r"make: \*\*\* \[.*\] Error \d+|"
    r"make: \*\*\* \[.*\] Interrupt",
    re.IGNORECASE,
)

# PowerShell and Ginkgo colourise output. Escape sequences sit between the words
# the patterns need to match, so they are stripped before matching.
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\[\d{1,2}(;\d{1,2})*m")

# Lines kept regardless of where they appear in the log.
#
# BATS emits TAP: a long run of `ok N ...` lines followed by the make error.
# Measured over a 25-run sample, the `not ok N` that explains the failure sits
# up to 943 lines above the ##[error] marker (median 186), well outside any
# practical context window. Structured failure lines are therefore matched
# across the whole log.
SALIENT = re.compile(
    r"^not ok \d+|"                       # TAP / BATS failure
    r"^# (from function|in test file)|"   # BATS failure context
    r"^\s*\[FAIL(ED)?\]|"                 # Ginkgo
    r"^--- FAIL: |"                       # go test
    r"^Summarizing \d+ Failure",
    re.MULTILINE,
)
SALIENT_CAP = 40

COMPILED = [
    (name, desc, [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in pats])
    for name, desc, pats in TAXONOMY
]

ERROR_MARKER = re.compile(r"##\[error\]")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?")


# --------------------------------------------------------------------------
# GitHub API client
# --------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface redirects as HTTPError so the caller can re-issue them unauthed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token: str, cache: Path = CACHE, verbose: bool = False):
        self.token = token
        self.cache = cache
        self.verbose = verbose
        self.cache.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _cache_path(self, key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:180]
        return self.cache / safe

    def _request(self, url: str, raw: bool) -> str | None:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "podman-flake-analyzer",
            },
        )
        # The job-logs endpoint 302s to blob storage, which rejects the request
        # if GitHub's Authorization header is forwarded. Catch the redirect and
        # follow it with a clean request instead.
        opener = urllib.request.build_opener(*([_NoRedirect] if raw else []))

        for attempt in range(4):
            try:
                with opener.open(req, timeout=90) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                if raw and e.code in (301, 302, 303, 307, 308):
                    loc = e.headers.get("Location")
                    if not loc:
                        return None
                    try:
                        clean = urllib.request.Request(
                            loc, headers={"User-Agent": "podman-flake-analyzer"}
                        )
                        with urllib.request.urlopen(clean, timeout=180) as r2:
                            return r2.read().decode("utf-8", errors="replace")
                    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
                        time.sleep(2 ** attempt)
                        continue
                # 410 = log expired and purged by GitHub retention; not retryable.
                if e.code in (404, 410):
                    return None
                if e.code in (403, 429):
                    reset = e.headers.get("X-RateLimit-Reset")
                    wait = 60
                    if reset:
                        wait = max(5, int(reset) - int(time.time()) + 5)
                    if self.verbose:
                        print(f"  rate limited, sleeping {wait}s", file=sys.stderr)
                    time.sleep(min(wait, 300))
                    continue
                if e.code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                return None
            except (urllib.error.URLError, TimeoutError):
                time.sleep(2 ** attempt)
        return None

    def get(self, path: str, raw: bool = False):
        url = path if path.startswith("http") else f"{API}/{path.lstrip('/')}"
        cp = self._cache_path(("raw_" if raw else "") + url.replace(API + "/", ""))
        if cp.exists():
            self.hits += 1
            text = cp.read_text(encoding="utf-8", errors="replace")
            if text == "\0MISSING\0":
                return None
            return text if raw else json.loads(text)

        self.misses += 1
        text = self._request(url, raw)
        cp.write_text("\0MISSING\0" if text is None else text, encoding="utf-8")
        if text is None:
            return None
        return text if raw else json.loads(text)


# Log handling


def extract_failure_region(
    log: str, before: int = 30, after: int = 4, max_chars: int = 12000
) -> str:
    """Pull the lines around each ##[error] annotation.

    A GitHub Actions job log is mostly setup noise; the signal sits near the
    annotations. Two things matter for getting a usable window:

    * Context is asymmetric. Most jobs end with a generic "Process completed
      with exit code 1" and the actual cause (the 504, the 429, the assertion)
      is above it, so the window extends further back than forward.
    * Generic markers do not anchor on their own. If a specific annotation
      exists anywhere in the log, only those are used as anchors.
    """
    lines = [ANSI.sub("", TIMESTAMP.sub("", ln)) for ln in log.splitlines()]

    all_errs = [i for i, ln in enumerate(lines) if ERROR_MARKER.search(ln)]
    specific = [i for i in all_errs if not GENERIC_ERRORS.search(lines[i])]
    idxs = specific or all_errs

    # Whole-log scan first. Position-independent, so TAP failures buried in
    # passing output still reach the classifier.
    salient = [ln for ln in lines if SALIENT.match(ln)][:SALIENT_CAP]

    if not idxs:
        window = lines[-80:]
    else:
        keep: set[int] = set()
        for i in idxs:
            keep.update(range(max(0, i - before), min(len(lines), i + after + 1)))
        window, last = [], None
        for i in sorted(keep):
            if last is not None and i > last + 1:
                window.append("   ...")
            window.append(lines[i])
            last = i

    parts = []
    if salient:
        parts.append("--- salient failure lines (whole-log scan) ---")
        parts.extend(salient)
        parts.append("--- log tail around ##[error] ---")
    parts.extend(window)
    return "\n".join(parts)[-max_chars:]


def classify(region: str) -> tuple[str, str]:
    """Return (category, the line that triggered the match)."""
    for name, _desc, pats in COMPILED:
        for pat in pats:
            m = pat.search(region)
            if not m:
                continue
            # Quote the specific line, never the generic "exit code 1" tail.
            line = next(
                (
                    ln.strip()
                    for ln in region.splitlines()
                    if pat.search(ln) and not GENERIC_ERRORS.search(ln)
                ),
                m.group(0),
            )
            return name, " ".join(line.split())[:300]
    return "unclassified", ""



# Analysis

@dataclass
class Failure:
    run_id: int
    run_attempt: int
    branch: str
    event: str
    created: str
    job_id: int
    job_name: str
    failed_steps: list[str]
    url: str
    category: str = "unclassified"
    evidence: str = ""
    rerun_passed: bool | None = None  # True = confirmed flake

    @property
    def on_main(self) -> bool:
        return self.branch in ("main", "master")


def collect_failed_runs(gh: GitHub, repo: str, workflow: str, limit: int) -> list[dict]:
    runs, page = [], 1
    while len(runs) < limit and page <= 10:
        data = gh.get(
            f"repos/{repo}/actions/workflows/{workflow}/runs"
            f"?status=failure&per_page=100&page={page}"
        )
        if not data or not data.get("workflow_runs"):
            break
        runs.extend(data["workflow_runs"])
        page += 1
    return runs[:limit]


def failed_jobs_for_run(gh: GitHub, repo: str, run: dict) -> list[Failure]:
    data = gh.get(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")
    if not data:
        return []
    out = []
    for job in data.get("jobs", []):
        if job.get("conclusion") != "failure":
            continue
        steps = [
            s["name"]
            for s in (job.get("steps") or [])
            if s.get("conclusion") == "failure"
        ]
        out.append(
            Failure(
                run_id=run["id"],
                run_attempt=run.get("run_attempt", 1),
                branch=run.get("head_branch") or "?",
                event=run.get("event") or "?",
                created=(run.get("created_at") or "")[:10],
                job_id=job["id"],
                job_name=job.get("name", "?"),
                failed_steps=steps,
                url=job.get("html_url", ""),
            )
        )
    return out


def detect_reruns(gh: GitHub, repo: str, failures: list[Failure]) -> list[tuple[int, str]]:
    """Find jobs that failed on one attempt and passed on the latest attempt.

    This is ground truth: no heuristics, no log parsing. Both directions are
    read from the API, so a job absent from a set is never inferred to have
    passed. Returns distinct (run_id, job_name) pairs.
    """
    confirmed: list[tuple[int, str]] = []

    by_run: dict[int, int] = {}
    for f in failures:
        if f.run_attempt > 1:
            by_run[f.run_id] = max(by_run.get(f.run_id, 0), f.run_attempt)

    for run_id, latest in by_run.items():
        attempts: dict[int, dict[str, str]] = {}
        for a in range(1, latest + 1):
            data = gh.get(
                f"repos/{repo}/actions/runs/{run_id}/attempts/{a}/jobs?per_page=100"
            )
            if data:
                attempts[a] = {
                    j["name"]: j.get("conclusion") for j in data.get("jobs", [])
                }
        if latest not in attempts:
            continue

        final = attempts[latest]
        for a in range(1, latest):
            for name, conclusion in attempts.get(a, {}).items():
                if conclusion == "failure" and final.get(name) == "success":
                    if (run_id, name) not in confirmed:
                        confirmed.append((run_id, name))

    marked = {name for _, name in confirmed}
    for f in failures:
        if f.job_name in marked:
            f.rerun_passed = True
    return confirmed


def enrich(gh: GitHub, repo: str, failures: list[Failure], workers: int) -> None:
    def work(f: Failure) -> None:
        log = gh.get(f"repos/{repo}/actions/jobs/{f.job_id}/logs", raw=True)
        if not log:
            f.category = "log_unavailable"
            return
        region = extract_failure_region(log)
        f.category, f.evidence = classify(region)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, failures))


# Reporting
def build_report(repo: str, workflow: str, failures: list[Failure],
                 confirmed: list[tuple[int, str]]) -> str:
    desc = {name: d for name, d, _ in TAXONOMY}
    desc.update(
        {
            "unclassified": "No taxonomy rule matched, candidate for a new rule",
            "log_unavailable": "Log purged by GitHub retention (410)",
        }
    )

    cats = Counter(f.category for f in failures)
    total = len(failures)
    infra_like = sum(
        cats[c]
        for c in ("infra_runner", "network_timeout", "registry_pull",
                  "dependency_install", "timing_race")
    )

    L = []
    A = L.append
    A(f"# CI failure analysis: `{repo}` / `{workflow}`\n")
    A(f"Sample: **{total} failed jobs** across "
      f"{len({f.run_id for f in failures})} failed workflow runs.\n")

    A("## Root-cause distribution\n")
    A("| Category | Jobs | Share | Meaning |")
    A("|---|---:|---:|---|")
    for cat, n in cats.most_common():
        A(f"| `{cat}` | {n} | {100*n/total:.1f}% | {desc.get(cat,'')} |")
    A("")
    A(f"**Environment-caused (infra/network/registry/dependency/timing): "
      f"{infra_like}/{total} = {100*infra_like/total:.1f}%**. These are flakes "
      f"rather than defects, and are the addressable target.\n")

    A("## Failures on `main`\n")
    main_f = [f for f in failures if f.on_main]
    if main_f:
        A(f"{len(main_f)} failed jobs landed on the default branch. `main` should "
          "be green, so each is either a real regression or a flake, making this "
          "the highest-value queue to triage.\n")
        A("| Date | Job | Category | Evidence |")
        A("|---|---|---|---|")
        for f in sorted(main_f, key=lambda x: x.created, reverse=True)[:15]:
            ev = f.evidence.replace("|", "\\|")[:90]
            A(f"| {f.created} | [{f.job_name[:40]}]({f.url}) | `{f.category}` | `{ev}` |")
        A("")
    else:
        A("No failures on the default branch in this sample.\n")

    A("## Most frequently failing jobs\n")
    jobs = Counter(f.job_name for f in failures)
    A("| Job | Failures | Categories seen |")
    A("|---|---:|---|")
    for name, n in jobs.most_common(15):
        seen = Counter(f.category for f in failures if f.job_name == name)
        A(f"| `{name[:52]}` | {n} | {', '.join(f'{c}×{k}' for c, k in seen.most_common(3))} |")
    A("")

    A("## Rerun-confirmed flakes\n")
    if confirmed:
        A(f"{len(confirmed)} job(s) failed on one attempt and passed on the latest "
          "attempt of the same run. This is ground truth: no heuristics, no log "
          "parsing, and the natural seed set for evaluating any classifier.\n")
        A("| Run | Job |")
        A("|---|---|")
        for rid, name in confirmed:
            A(f"| [{rid}](https://github.com/{repo}/actions/runs/{rid}) | `{name}` |")
    else:
        A("None in this sample. Reruns are the only zero-heuristic flake signal "
          "available, and they are rare, which is a limitation of this approach.")
    A("")

    unc = [f for f in failures if f.category == "unclassified"]
    if unc:
        A("## Unclassified\n")
        A(f"{len(unc)} job(s) matched no rule. In the full system these are the "
          "queue an LLM stage should triage, with its output reviewed and folded "
          "back into the taxonomy.\n")
        for f in unc[:8]:
            A(f"- [{f.job_name[:60]}]({f.url}), steps: "
              f"{', '.join(f.failed_steps[:2]) or 'n/a'}")
        A("")

    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default="podman-container-tools/podman")
    ap.add_argument("--workflow", default="ci.yml")
    ap.add_argument("--limit", type=int, default=60, help="failed runs to ingest")
    ap.add_argument(
        "--exclude-jobs",
        default=r"^(Total Success|All .* passed|CI Success)$",
        help="regex of aggregator/gate jobs to drop. They mirror other jobs' "
             "failures and would double-count.",
    )
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default="flake-report.md")
    ap.add_argument("--json-out", default="")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    try:
        token = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("No token. Run `gh auth login` or set GITHUB_TOKEN.", file=sys.stderr)
        return 1

    gh = GitHub(token, verbose=args.verbose)

    print(f"[1/4] ingesting failed runs for {args.repo}/{args.workflow} ...")
    runs = collect_failed_runs(gh, args.repo, args.workflow, args.limit)
    print(f"      {len(runs)} failed runs")

    print("[2/4] resolving failed jobs ...")
    failures: list[Failure] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for got in ex.map(lambda r: failed_jobs_for_run(gh, args.repo, r), runs):
            failures.extend(got)
    gate = re.compile(args.exclude_jobs)
    dropped = [f for f in failures if gate.match(f.job_name)]
    failures = [f for f in failures if not gate.match(f.job_name)]
    print(f"      {len(failures)} failed jobs "
          f"({len(dropped)} aggregator/gate jobs excluded)")

    if not failures:
        print("nothing to analyze")
        return 0

    print("[3/4] fetching logs and classifying ...")
    enrich(gh, args.repo, failures, args.workers)
    confirmed = detect_reruns(gh, args.repo, failures)

    print("[4/4] writing report ...")
    report = build_report(args.repo, args.workflow, failures, confirmed)
    Path(args.out).write_text(report, encoding="utf-8")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                [
                    {k: v for k, v in f.__dict__.items()}
                    for f in failures
                ],
                indent=2,
            ),
            encoding="utf-8",
        )

    print(f"      wrote {args.out}  (cache: {gh.hits} hits / {gh.misses} fetches)")
    print()
    print(report[: report.find("## Failures on")])
    return 0


if __name__ == "__main__":
    sys.exit(main())
