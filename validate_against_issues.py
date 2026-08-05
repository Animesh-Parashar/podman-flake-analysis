#!/usr/bin/env python3
"""Validate a flake ranking against Podman's maintainer-labelled `flakes` issues.

This is the external ground truth the ranking never sees. Podman maintainers
hand-label issues with `flakes` (376 at time of writing, 334 of them closed),
most naming the test in the title as `<context>: <error>`. If a ranking derived
purely from CI outcome history surfaces tests maintainers independently filed
issues about, that is evidence the detector measures what they mean by "flake".

Use the FULL corpus, open and closed. Restricting to open issues drops 89% of
it and silently excludes flakes that were triaged and fixed, which are still
maintainer judgements about what counts as a flake.

Rerun pairs, the only zero-heuristic flake signal available from the Actions
API, are rare: an earlier pass over this repository surfaced just two. This
corpus is the alternative ground truth.

Matching is deliberately conservative and reported with its false-positive
behaviour, since a generous matcher would manufacture the result it is meant
to test.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Words that carry no discriminating power in this corpus: every issue and
# every test contains them, so they inflate any overlap score.
STOP = set("""
the a an and or of to in is are was were be been it its this that with for on
at by from as not no test tests ci flake flaky flakes podman error failure
fail failed run runs system e2e remote local root rootless bug issue
""".split())


def toks(s: str) -> set[str]:
    s = (s or "").lower()
    s = re.sub(r"\[\d+\]", " ", s)            # TAP suite prefix [610]
    s = re.sub(r"\|\d+\|", " ", s)            # variant prefix |610|
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return {t for t in s.split() if len(t) > 2 and t not in STOP}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ranking", required=True, help="JSON from rank_flakes.py")
    ap.add_argument("--issues", required=True, help="JSON list of issues with number/title")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--thresh", type=float, default=0.5,
                    help="fraction of issue tokens that must appear in the test id")
    ap.add_argument("--min-tok", type=int, default=3)
    a = ap.parse_args()

    ranking = json.loads(Path(a.ranking).read_text())
    issues = json.loads(Path(a.issues).read_text())

    # Signature per issue: the title is the maintainer's own summary and is
    # written as `<context>: <error>`, so it is the best single source.
    sigs = []
    for i in issues:
        t = toks(i["title"])
        if len(t) >= a.min_tok:
            sigs.append((i["number"], i["title"], t))
    print(f"issues usable as signatures: {len(sigs)}/{len(issues)}")
    print(f"ranking entries: {len(ranking)}, checking top {a.top}\n")

    hits = []
    for rank, r in enumerate(ranking[:a.top], 1):
        tt = toks(r["test"])
        if not tt:
            continue
        best = None
        for num, title, st in sigs:
            ov = len(st & tt) / len(st)
            if ov >= a.thresh and (best is None or ov > best[0]):
                best = (ov, num, title)
        if best:
            hits.append((rank, r["test"], best))

    print("=" * 72)
    print(f"MATCHES IN TOP {a.top} (threshold {a.thresh})")
    print("=" * 72)
    for rank, test, (ov, num, title) in hits:
        print(f"#{rank:<3} ov={ov:.2f}  test : {test[:66]}")
        print(f"           issue #{num}: {title[:66]}")
    print(f"\n{len(hits)} of top {a.top} map to a maintainer-filed flakes issue")

    # Negative control: the same matcher against randomly drawn tests tells us
    # how many "hits" the threshold produces by chance alone. Without this the
    # hit count means nothing.
    import random
    random.seed(0)
    pool = [r["test"] for r in ranking]
    trials, chance = 200, 0
    for _ in range(trials):
        sample = random.sample(pool, min(a.top, len(pool)))
        for test in sample:
            tt = toks(test)
            if any(len(st & tt) / len(st) >= a.thresh for _, _, st in sigs):
                chance += 1
    print(f"negative control: {chance/trials:.2f} expected hits per {a.top} "
          f"randomly drawn tests ({trials} draws)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
