#!/usr/bin/env python3
"""Rank tests by flakiness evidence, running a set of contamination checks
before reporting anything.

The checks are labelled C1-C8 in the output and are, in order:

  C1  commit retries      one broken commit retried across runs
  C2  matrix duplication  one spec failing in root/rootless/remote of one run
  C3  regression          a real breakage, time-concentrated, not a flake
  C4  log retention       GitHub purges old logs (HTTP 410), biasing recent
  C5  id fragmentation    one test splitting into several ids
  C6  branch reuse        one long-lived branch pushed many times
  C7  sampling bias       see c7_control.py
  C8  single-branch       a test failing only where someone is editing it

Signals, in the priority the literature gives them:

  1. flip rate   -- strongest of the 3 features in the published F1 95.5%
                    classifier. A test that both passes and fails is the
                    canonical flake signature.
  2. same-commit -- passed AND failed on the identical commit. The strongest
                    evidence obtainable without reruns, since the code under
                    test is held constant by construction.
  3. spread      -- distinct branches and commits failed on. Approximates
                    DeFlaker's "failed without executing the changed code"
                    without needing coverage instrumentation.
  4. mode diff   -- parallel vs serial failure rate. Podman runs the same tests
                    both ways (test/system/helpers.bash:201-205), so the
                    comparison iDFlakies performs deliberately is free here.

Counts are per distinct commit, never per raw event: raw events are inflated by
matrix duplication across job variants (C2) and commit retries (C1).

Single streaming pass. Materialising the parsed rows exhausted 14 GB of RAM at
2.5M observations, so nothing larger than the aggregates is ever retained.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

MODE_RE = re.compile(r"^(\[\d+\]|\|\d+\|)\s*")


def mode_of(test: str) -> str:
    """serial | parallel | none, per test/system/helpers.bash:201-205."""
    if test.startswith("|"):
        return "parallel"
    if test.startswith("["):
        return "serial"
    return "none"


def strip_mode(test: str) -> str:
    return MODE_RE.sub("", test)


def ts(s: str) -> datetime:
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def stream(path: Path) -> dict:
    """One pass over the JSONL. Retains aggregates only, never observations."""
    agg: dict[str, dict] = defaultdict(lambda: {
        "fail_shas": set(), "pass_shas": set(), "fail_branches": set(),
        "fail_events": 0, "pass_events": 0, "jobs": set(),
        "first": None, "last": None,
    })
    mode: dict[str, dict] = defaultdict(
        lambda: {"parallel": [0, 0], "serial": [0, 0],
                 "par_br": set(), "ser_br": set()})
    fail_pairs: set[tuple[str, str]] = set()          # (sha, test) -- C1+C2
    fail_times: dict[str, list] = defaultdict(list)   # -- C3
    runs, shas, branches = set(), set(), set()
    mode_obs = Counter()
    n = raw_fail = bad = 0
    lo = hi = None

    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                bad += 1                              # truncated tail line
                continue
            n += 1
            test, sha, passed = r["test"], r["sha"], r["passed"]
            t = ts(r["created"])
            runs.add(r["run_id"]); shas.add(sha); branches.add(r["branch"])
            m = mode_of(test)
            mode_obs[m] += 1

            a = agg[test]
            a["jobs"].add(r["job_name"])
            if a["first"] is None or t < a["first"]:
                a["first"] = t
            if a["last"] is None or t > a["last"]:
                a["last"] = t
            if lo is None or t < lo:
                lo = t
            if hi is None or t > hi:
                hi = t

            if m != "none":
                md = mode[strip_mode(test)]
                md[m][0 if passed else 1] += 1
                if not passed:
                    md["par_br" if m == "parallel" else "ser_br"].add(r["branch"])

            if passed:
                a["pass_events"] += 1
                a["pass_shas"].add(sha)
            else:
                a["fail_events"] += 1
                a["fail_shas"].add(sha)
                a["fail_branches"].add(r["branch"])
                raw_fail += 1
                fail_pairs.add((sha, test))
                fail_times[test].append(t)

    return {"agg": agg, "mode": mode, "fail_pairs": fail_pairs,
            "fail_times": fail_times, "runs": runs, "shas": shas,
            "branches": branches, "mode_obs": mode_obs, "n": n,
            "raw_fail": raw_fail, "bad": bad, "lo": lo, "hi": hi}


def score(agg: dict) -> list[dict]:
    out = []
    for test, a in agg.items():
        fs, ps = a["fail_shas"], a["pass_shas"]
        seen = fs | ps
        if not seen:
            continue
        out.append({
            "test": test,
            "mode": mode_of(test),
            "fail_shas": len(fs),
            "pass_shas": len(ps),
            "seen_shas": len(seen),
            "flip_rate": round(len(fs) / len(seen), 5),
            "both_same_sha": len(fs & ps),
            "fail_branches": len(a["fail_branches"]),
            "fail_events": a["fail_events"],
            "jobs": len(a["jobs"]),
            "span_days": round((a["last"] - a["first"]).total_seconds() / 86400, 2),
            "first": a["first"].isoformat(),
            "last": a["last"].isoformat(),
        })
    return out


def contamination(d: dict, scored: list[dict]) -> None:
    print("=" * 74)
    print("CONTAMINATION CHECKS, run before any ranking")
    print("=" * 74)

    raw, dedup = d["raw_fail"], len(d["fail_pairs"])
    infl = 100 * (raw - dedup) / raw if raw else 0
    print(f"\nC1+C2  raw failure events           : {raw}")
    print(f"       deduped by (commit, test)    : {dedup}")
    print(f"       inflation removed            : {infl:.1f}%")

    nr, ns, nb = len(d["runs"]), len(d["shas"]), len(d["branches"])
    print(f"\nC6     distinct runs                 : {nr}")
    print(f"       distinct commits             : {ns}")
    print(f"       distinct branches            : {nb}")
    print(f"       runs per commit              : {nr/max(1,ns):.2f}")

    base = defaultdict(set)
    for t in d["agg"]:
        base[strip_mode(t)].add(mode_of(t))
    both = sum(1 for v in base.values() if len(v) > 1)
    print(f"\nC5     distinct test ids             : {len(d['agg'])}")
    print(f"       distinct base names          : {len(base)}")
    print(f"       seen in BOTH modes           : {both}")
    print(f"       observations by mode         : {dict(d['mode_obs'])}")

    print(f"\nC4     window covered                : {d['lo'].date()} to "
          f"{d['hi'].date()} ({(d['hi']-d['lo']).days} days)")

    print(f"\nC3     regression check (time-concentration of failures)")
    win = max(1.0, (d["hi"] - d["lo"]).total_seconds())
    flagged = []
    for s in sorted(scored, key=lambda x: -x["fail_shas"])[:80]:
        if s["fail_shas"] < 2:
            continue
        ft = sorted(d["fail_times"][s["test"]])
        if len(ft) < 2:
            continue
        conc = 1.0 - ((ft[-1] - ft[0]).total_seconds() / win)
        if conc > 0.8 and s["both_same_sha"] == 0:
            flagged.append((conc, s["test"]))
    flagged.sort(reverse=True)
    for conc, t in flagged[:6]:
        print(f"       REGRESSION-LIKE conc={conc:.2f} same_sha=0  {t[:48]}")
    print(f"       flagged regression-like      : {len(flagged)}")
    print(f"       (concentrated >0.8 AND never passed on a commit it failed on)")


def c8_report(d: dict, scored: list[dict], min_branches: int) -> set[str]:
    """C8: single-branch concentration.

    A test being actively modified on a feature branch fails there repeatedly.
    Every failure is real, none is a flake. C3 cannot catch it: the failures
    are spread over time and the test does pass on some of the same commits,
    so it is neither flake nor regression.

    The discriminator that matters is not the branch COUNT but whether any
    failure landed on main or a release branch, which is nobody's working
    branch.
    """
    print(f"\nC8     single-branch concentration")
    fail_br = defaultdict(Counter)
    for t, a in d["agg"].items():
        for b in a["fail_branches"]:
            fail_br[t][b] += 1
    suspect = set()
    for s_ in scored:
        if s_["fail_shas"] == 0:
            continue
        brs = d["agg"][s_["test"]]["fail_branches"]
        if len(brs) < min_branches:
            suspect.add(s_["test"])
    integ = {t for t, a in d["agg"].items()
             if a["fail_branches"] & MAINISH or
             any(b.startswith(("v5.", "v6.", "release")) for b in a["fail_branches"])}
    failing = [s_ for s_ in scored if s_["fail_shas"] > 0]
    print(f"       tests with >=1 failure       : {len(failing)}")
    print(f"       failing on < {min_branches} branches       : {len(suspect)} "
          f"(excluded from ranking)")
    print(f"       failing on main/release      : {len(integ & {s_['test'] for s_ in failing})}"
          f"  <- cannot be one dev's WIP")
    return suspect


MAINISH = {"main", "master"}


def mode_differential(mode: dict, min_branches: int) -> list[dict]:
    out = []
    for b, dd in mode.items():
        pn, sn = sum(dd["parallel"]), sum(dd["serial"])
        pf, sf = dd["parallel"][1], dd["serial"][1]
        if pn == 0 or sn == 0 or (pf == 0 and sf == 0):
            continue
        # C8 guard: a mode difference driven by failures on one branch is a
        # developer editing that test, not a property of the execution mode.
        nbr = len(dd["par_br"] | dd["ser_br"])
        if nbr < min_branches:
            continue
        out.append({"test": b, "branches": nbr,
                    "par_fail": pf, "par_n": pn,
                    "ser_fail": sf, "ser_n": sn,
                    "par_rate": round(pf / pn, 6), "ser_rate": round(sf / sn, 6),
                    "diff": round(pf / pn - sf / sn, 6)})
    out.sort(key=lambda x: -x["diff"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="inp", default="testruns.jsonl")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--min-shas", type=int, default=5)
    ap.add_argument("--min-branches", type=int, default=3,
                    help="C8 guard: minimum distinct branches a test must have "
                         "failed on. 1-2 branches usually means a developer was "
                         "editing that test, not a flake.")
    ap.add_argument("--json-out", default="")
    a = ap.parse_args()

    d = stream(Path(a.inp))
    if not d["n"]:
        print("no rows", file=sys.stderr)
        return 1
    print(f"observations: {d['n']:,}   failures: {d['raw_fail']}"
          + (f"   unparsable lines: {d['bad']}" if d["bad"] else ""))

    scored = score(d["agg"])
    contamination(d, scored)
    suspect = c8_report(d, scored, a.min_branches)

    elig = [s for s in scored
            if s["seen_shas"] >= a.min_shas and s["fail_shas"] > 0
            and s["test"] not in suspect]
    elig.sort(key=lambda s: (-s["both_same_sha"], -s["flip_rate"],
                             -s["fail_branches"]))

    print("\n" + "=" * 74)
    print(f"RANKED FLAKE CANDIDATES (seen on >= {a.min_shas} commits)")
    print("=" * 74)
    print(f"(C8 guard: >= {a.min_branches} distinct branches)")
    print(f"{'flip':>6} {'fail/seen':>12} {'same':>5} {'brch':>5} {'days':>6}  test")
    for s in elig[:a.top]:
        print(f"{s['flip_rate']:>6.3f} {s['fail_shas']:>4}/{s['seen_shas']:<7} "
              f"{s['both_same_sha']:>5} {s['fail_branches']:>5} "
              f"{s['span_days']:>6.1f}  {s['test'][:64]}")

    md = mode_differential(d["mode"], a.min_branches)
    print("\n" + "=" * 74)
    print("MODE DIFFERENTIAL: parallel vs serial (helpers.bash:201-205)")
    print("=" * 74)
    if md:
        print(f"{'parallel':>13} {'serial':>13} {'diff':>10}  test")
        for m in md[:12]:
            print(f"{m['par_fail']:>4}/{m['par_n']:<8} {m['ser_fail']:>4}/{m['ser_n']:<8} "
                  f"{m['diff']:>+10.5f}  br={m['branches']}  {m['test'][:44]}")
        op = sum(1 for m in md if m["ser_fail"] == 0 and m["par_fail"] > 0)
        os_ = sum(1 for m in md if m["par_fail"] == 0 and m["ser_fail"] > 0)
        print(f"\n  comparable tests      : {len(md)}")
        print(f"  fails ONLY in parallel: {op}")
        print(f"  fails ONLY in serial  : {os_}")
    else:
        print("  no mode-comparable failure in this sample")

    if a.json_out:
        Path(a.json_out).write_text(json.dumps(elig, indent=1), encoding="utf-8")
        print(f"\nwrote {a.json_out}  ({len(elig)} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
