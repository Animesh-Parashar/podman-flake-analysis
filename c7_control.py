#!/usr/bin/env python3
"""C7: bound the failed-run sampling bias in the flip rates.

Walking only FAILED runs is what makes the collection affordable, because a
BATS log lists `ok N` for passing tests as well, so one failed job still yields
the full pass/fail vector for every test in it. This bias was identified and
its test designed before any data was collected.

But the denominator is then "runs in which something failed", not "all runs".
Every flip rate computed on that sample is therefore an UPPER BOUND on the
test's true flip rate. Relative ranking survives, since the bias applies to
every test in the same jobs. Absolute claims of the form "test X is flaky 12%
of the time" do not.

This measures the size of that bias by adding a control sample of SUCCESSFUL
runs, which contribute passing observations only, and reporting the deflation.

Usage:
  c7_control.py --failed fail_runs.jsonl --success success_runs.jsonl \
                --total-failed 441 --total-success 662
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


def load_counts(path: Path) -> tuple[dict, set]:
    """(test -> {fail_shas, pass_shas}), and the set of commits seen."""
    per: dict[str, dict] = defaultdict(lambda: {"f": set(), "p": set()})
    shas: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            shas.add(r["sha"])
            d = per[r["test"]]
            d["p" if r["passed"] else "f"].add(r["sha"])
    return per, shas


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--failed", required=True)
    ap.add_argument("--success", required=True)
    ap.add_argument("--total-failed", type=int, default=441,
                    help="failed runs in the population")
    ap.add_argument("--total-success", type=int, default=662,
                    help="successful runs in the population")
    ap.add_argument("--top", type=int, default=12)
    a = ap.parse_args()

    fper, fshas = load_counts(Path(a.failed))
    sper, sshas = load_counts(Path(a.success))

    sfails = sum(len(d["f"]) for d in sper.values())
    print(f"failed-run sample   : {len(fshas)} commits, {len(fper)} tests")
    print(f"success-run control : {len(sshas)} commits, {len(sper)} tests")
    print(f"failures in control : {sfails}  (expected 0: a run is 'success' "
          f"only if every job passed)")

    # Scale factor: the control covers a fraction of the successful population.
    frac = len(sshas) / max(1, a.total_success)
    print(f"\ncontrol covers {len(sshas)}/{a.total_success} successful runs "
          f"= {100*frac:.0f}%")

    rows = []
    for test, d in fper.items():
        f = len(d["f"])
        if f == 0:
            continue
        seen_fail_sample = len(d["f"] | d["p"])
        extra = len(sper.get(test, {"p": set()})["p"] - d["f"] - d["p"])
        seen_combined = seen_fail_sample + extra
        # population estimate: scale the control's extra passing commits up
        extra_pop = extra / frac if frac > 0 else extra
        seen_pop = seen_fail_sample + extra_pop
        rows.append({
            "test": test, "fails": f,
            "flip_failed_only": f / seen_fail_sample,
            "flip_combined": f / seen_combined if seen_combined else 0,
            "flip_pop_est": f / seen_pop if seen_pop else 0,
            "seen_fail": seen_fail_sample, "seen_comb": seen_combined,
        })
    rows.sort(key=lambda r: -r["fails"])

    if not rows:
        print("\nno failing tests in the failed sample", file=sys.stderr)
        return 1

    print("\n" + "=" * 78)
    print("FLIP RATE: failed-run sample vs combined vs population estimate")
    print("=" * 78)
    print(f"{'failed-only':>12} {'combined':>10} {'pop est':>10} {'ratio':>7}  test")
    for r in rows[:a.top]:
        ratio = (r["flip_failed_only"] / r["flip_pop_est"]
                 if r["flip_pop_est"] else float("inf"))
        print(f"{r['flip_failed_only']:>12.4f} {r['flip_combined']:>10.4f} "
              f"{r['flip_pop_est']:>10.4f} {ratio:>7.2f}x  {r['test'][:44]}")

    med = sorted(r["flip_failed_only"] / r["flip_pop_est"]
                 for r in rows if r["flip_pop_est"] > 0)
    if med:
        m = med[len(med) // 2]
        print(f"\nmedian overstatement factor: {m:.2f}x")
        print("Flip rates measured on failed runs alone overstate the true rate")
        print(f"by roughly {m:.1f}x. RANKING is unaffected (the bias is common to")
        print("all tests in the same jobs); ABSOLUTE rates must use the estimate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
