#!/usr/bin/env python3
"""Put a local LLM on the failures the rule taxonomy could not name.

The rule classifier in flake_analyzer.py assigns a category to every failure it
recognises and `unclassified` to the rest. Those leftovers are the natural
queue for a model stage: the rules have already handled everything with a clear
textual signature, so what remains is the set where reading comprehension might
actually add something.

Two sets are run:

  TARGET   jobs the rules left `unclassified`. No ground truth exists, so every
           answer must be read by hand. That is the point of the set.
  CONTROL  jobs the rules classified confidently. Used only to check the model
           agrees on easy cases. If it does not, nothing it says about the
           target set is worth reading.

Uses Ollama over its local HTTP API. Local rather than a hosted model on
purpose: a flake-triage tool that ships CI logs to a third party is a harder
thing for maintainers to adopt than one that does not.

  llm_triage.py --failures live_after.json --model qwen2.5:7b --out triage.json
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from flake_analyzer import (  # noqa: E402
    TAXONOMY, GitHub, extract_failure_region,
)

CATEGORIES = [name for name, _desc, _pats in TAXONOMY]
DESCRIPTIONS = {name: desc for name, desc, _ in TAXONOMY}

SYSTEM = (
    "You classify continuous-integration failures. You are given an excerpt of "
    "a GitHub Actions job log. Decide which category best explains why the job "
    "failed.\n\n"
    "Reply with STRICT JSON and nothing else:\n"
    '{"category": "<one of the listed categories, or insufficient_information>",'
    ' "reason": "<one sentence, plain English>"}\n\n'
    "Use insufficient_information when the excerpt does not actually show the "
    "cause. That is a correct and useful answer, not a failure. Do not guess a "
    "specific cause that the text does not support."
)


def build_prompt(region: str, max_chars: int) -> str:
    cats = "\n".join(f"- {c}: {DESCRIPTIONS[c]}" for c in CATEGORIES)
    excerpt = region[-max_chars:] if len(region) > max_chars else region
    return (
        f"Categories:\n{cats}\n- insufficient_information: the excerpt does not "
        f"show the cause\n\n"
        f"Log excerpt:\n```\n{excerpt}\n```\n\n"
        f"JSON only."
    )


def ollama(model: str, prompt: str, num_ctx: int, host: str) -> tuple[str, dict]:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_ctx": num_ctx, "temperature": 0},
    }).encode()
    req = urllib.request.Request(
        f"{host}/api/chat", data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.loads(r.read().decode())
    return d["message"]["content"], {
        "secs": round(time.time() - t0, 1),
        "prompt_tokens": d.get("prompt_eval_count"),
        "gen_tokens": d.get("eval_count"),
    }


def parse_reply(text: str) -> dict:
    """Models wrap JSON in prose or fences. Recover it rather than fail."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"category": "PARSE_FAILED", "reason": text[:160]}
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"category": "PARSE_FAILED", "reason": text[:160]}
    return {"category": str(d.get("category", "?")),
            "reason": str(d.get("reason", ""))[:300]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--failures", required=True,
                    help="JSON from flake_analyzer.py --json-out")
    ap.add_argument("--repo", default="podman-container-tools/podman")
    ap.add_argument("--model", default="qwen2.5:7b")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--num-ctx", type=int, default=4096)
    ap.add_argument("--max-chars", type=int, default=6000,
                    help="truncate the log region; keeps the tail, where the "
                         "##[error] anchor and its context sit")
    ap.add_argument("--control", type=int, default=10,
                    help="how many rule-classified jobs to use for calibration")
    ap.add_argument("--limit-target", type=int, default=0,
                    help="0 = all unclassified; >0 for a pilot")
    ap.add_argument("--out", default="triage.json")
    a = ap.parse_args()

    data = json.loads(Path(a.failures).read_text())
    failures = data["failures"] if isinstance(data, dict) else data

    target = [f for f in failures if f["category"] == "unclassified"]
    classified = [f for f in failures
                  if f["category"] not in ("unclassified", "log_unavailable")]
    # spread the control across categories rather than taking the first N
    seen, control = set(), []
    for f in classified:
        if f["category"] not in seen:
            seen.add(f["category"]); control.append(f)
    for f in classified:
        if len(control) >= a.control:
            break
        if f not in control:
            control.append(f)
    control = control[:a.control]
    if a.limit_target:
        target = target[:a.limit_target]

    print(f"target (unclassified): {len(target)}", file=sys.stderr)
    print(f"control (rule-classified): {len(control)}", file=sys.stderr)
    print(f"model={a.model} num_ctx={a.num_ctx} max_chars={a.max_chars}",
          file=sys.stderr)

    try:
        tok = subprocess.run(["gh", "auth", "token"], capture_output=True,
                             text=True, check=True).stdout.strip()
    except Exception:
        tok = os.environ.get("GITHUB_TOKEN", "")
    gh = GitHub(tok)

    results = []
    for kind, rows in (("control", control), ("target", target)):
        for i, f in enumerate(rows, 1):
            log = gh.get(
                f"repos/{a.repo}/actions/jobs/{f['job_id']}/logs", raw=True)
            if not log:
                print(f"  [{kind} {i}/{len(rows)}] log unavailable",
                      file=sys.stderr)
                continue
            region = extract_failure_region(log)
            del log
            try:
                raw, meta = ollama(a.model, build_prompt(region, a.max_chars),
                                   a.num_ctx, a.host)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                print(f"  [{kind} {i}/{len(rows)}] model error: {e}",
                      file=sys.stderr)
                continue
            ans = parse_reply(raw)
            rec = {
                "kind": kind,
                "job_id": f["job_id"], "job_name": f["job_name"],
                "url": f.get("url", ""),
                "rule_category": f["category"],
                "rule_evidence": (f.get("evidence") or "")[:200],
                "llm_category": ans["category"],
                "llm_reason": ans["reason"],
                "agrees": (ans["category"] == f["category"]),
                **meta,
                "region_chars": len(region),
                "region_tail": region[-1200:],   # kept so answers are checkable
            }
            results.append(rec)
            mark = "=" if rec["agrees"] else " "
            print(f"  [{kind} {i}/{len(rows)}] {meta['secs']:>6}s {mark} "
                  f"rule={f['category']:<16} llm={ans['category']}",
                  file=sys.stderr)
            Path(a.out).write_text(json.dumps(results, indent=1))

    ctrl = [r for r in results if r["kind"] == "control"]
    if ctrl:
        agree = sum(1 for r in ctrl if r["agrees"])
        print(f"\nCONTROL agreement: {agree}/{len(ctrl)} = "
              f"{100*agree/len(ctrl):.0f}%  (pre-registered floor: 70%)",
              file=sys.stderr)
    tgt = [r for r in results if r["kind"] == "target"]
    if tgt:
        named = sum(1 for r in tgt
                    if r["llm_category"] not in
                    ("insufficient_information", "PARSE_FAILED", "unclassified"))
        print(f"TARGET: {named}/{len(tgt)} given a specific category "
              f"({100*named/len(tgt):.0f}%). ALL REQUIRE HAND-CHECKING.",
              file=sys.stderr)
    print(f"wrote {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
