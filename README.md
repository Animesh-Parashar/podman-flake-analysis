# CI failure categorization for Podman

A prototype of the ingestion and classification stage described in
[cncf/mentoring#1963](https://github.com/cncf/mentoring/issues/1963)

Built against real `podman-container-tools/podman` CI data, to establish what
the failures actually look like before deciding how to classify them.

## Usage

```bash
gh auth login
python3 flake_analyzer.py --limit 25
```

Standard library only, no third-party dependencies.

| Flag | Default | Meaning |
|---|---|---|
| `--repo` | `podman-container-tools/podman` | target repository |
| `--workflow` | `ci.yml` | workflow file to sample |
| `--limit` | 60 | failed runs to ingest |
| `--exclude-jobs` | `^(Total Success\|...)$` | aggregator/gate jobs to drop |
| `--out` | `flake-report.md` | markdown report |
| `--json-out` | none | `{failures: [...], rerun_confirmed: [...]}` for further analysis |

Logs are cached under `.cache/` (gitignored). Podman job logs run to roughly
22 MB each, so a 25-run sample is about 750 MB on disk. The cache makes reruns
free while iterating on the taxonomy.

## Pipeline

1. Ingest failed runs for a workflow from the Actions API.
2. Resolve each run to its failed jobs and failed steps.
3. Fetch job logs and extract the failure region.
4. Classify against a rule taxonomy.
5. Cross-check against rerun history.
6. Emit a markdown report.

## Design notes

Four behaviours were only discoverable by running against real logs. Each one
breaks a naive implementation.

**`make: *** [Makefile:705: ginkgo] Error 2` is a symptom, not a cause.**
Podman runs its test suites through make targets (`ginkgo`, `localsystem`,
`localmachine`), so matching that line as a build failure mislabels test
failures. In an intermediate version that combined a wide context window with a
`make:` rule, it drove `build_artifact` to 70.5% of the sample. It now sits in
`GENERIC_ERRORS` alongside `Process completed with exit code N`: terminal
markers that report that something failed without reporting what.

**The failure line can sit well above the `##[error]` marker.** Podman's system
tests are BATS, which emits TAP: a long run of `ok N ...` lines, then the make
error at the end. Measured across the sample, the `not ok N` explaining the
failure sits between 48 and 943 lines above the marker, median 186. That is
outside any practical context window, so the extractor also runs a
position-independent whole-log scan for structured failure lines (`not ok`,
`--- FAIL:`, Ginkgo `[FAILED]`).

**Context has to be asymmetric.** Most jobs end with a generic marker and the
real cause is above it, hence 30 lines back versus 4 forward, with a preference
for anchoring on specific annotations wherever any exist.

**Aggregator jobs double-count.** The `Total Success` gate job fails whenever
any other job fails. It accounted for 25 of 120 failed jobs, 20.8% of the raw
sample. Excluded by default.

PowerShell and Ginkgo also colourise output, and the ANSI escapes sit between
the words the patterns need to match, so regions are stripped before matching.

## Measured effect

Same 95 failed jobs, initial rule set versus current:

| | initial | current |
|---|---:|---:|
| unclassified | 85.3% | 5.3% |

## Why rules rather than a model, at this stage

The classifier is deterministic regex, which is deliberate and temporary.

A model that categorizes flakes needs something to be measured against, and no
labelled corpus of Podman CI failures currently exists. Rules produce that
corpus cheaply along with a baseline accuracy figure. Rerun-confirmed flakes
are ground truth requiring no heuristics at all, which makes them the natural
seed set for evaluating whatever replaces this.

The `unclassified` bucket is the interface to a model stage: those are the
failures the rules cannot name, which is the queue worth spending inference on,
with output reviewed and folded back into the taxonomy.

## Sample findings

25 failed `ci.yml` runs, 95 failed jobs, July 2026.

| Category | Share |
|---|---:|
| `test_assertion` | 68.4% |
| `timing_race` | 13.7% |
| `unclassified` | 5.3% |
| `size_regression` | 4.2% |
| `build_artifact` | 3.2% |
| `lint_static` | 2.1% |
| `network_timeout` | 2.1% |
| `github_infra` | 1.1% |

Environment-caused (network, infra, timing): **16.8%**.

Two specifics:

- Run
  [30107532084](https://github.com/podman-container-tools/podman/actions/runs/30107532084)
  on `main` failed four jobs with three distinct infrastructure causes in one
  run: a 429 from `codeload.github.com` while downloading `actions/checkout`,
  and two separate 504s fetching release assets.
- Seven jobs failed on `Unexpected warnings seen on stderr: "Trying to pull
  quay.io/libpod/..."`, which looks like a registry retry surfacing as a test
  assertion. This argues for classifying on the underlying log signature rather
  than on which assertion tripped.

## Limitations

- 25 runs is a small sample, chosen to keep the log cache manageable. The
  distribution above is indicative, not authoritative.
- Only 2 rerun-confirmed flakes appeared in the sample, both
  `Validate source code changes`. Reruns are the only zero-heuristic flake
  signal available and they are rare, which limits how far this cross-check
  can be pushed.
- A rerun-confirmed job passed on its run's latest attempt, so it is not a
  failure of that run and normally has no record in the failure set. The seed
  set is the `rerun_confirmed` list, not the `rerun_passed` column, which is
  empty in this sample by construction. Matching those confirmations back onto
  failures by job name rather than by `(run_id, job_name)` silently marks
  unrelated runs, since job names repeat across every run.
- `timing_race` is ordered before `test_assertion`, so a Ginkgo test failing on
  a timeout classifies as timing rather than assertion. That is a deliberate
  choice for flake-hunting and not obviously the right default for other uses.
- GitHub purges logs on a retention schedule. Older runs return 410 and are
  reported as `log_unavailable`.
- Windows job logs contain wide-character spacing (`F o r  m o r e`) that
  defeats naive matching. Not yet normalized.
- The taxonomy was developed against this sample, so the 5.3% unclassified
  figure is measured on the data it was fitted to.

## Out-of-sample check

The unmodified analyzer was pointed at `kyverno/kyverno`
(`check-unit-tests.yaml`, 25 failed jobs), a Go repository with a different
test harness, as a holdout for the caveat above.

| | Podman (fitted) | Kyverno (holdout) |
|---|---:|---:|
| `unclassified` | 5.3% | 4.0% |
| environment-caused | 16.8% | 0.0% |

All 25 evidence strings were read rather than trusted, since `build_artifact`
landed at 44% and that is the category the `make:` bug once inflated to 70.5%.
The labels were correct: Go compile errors against `--- FAIL:` assertions.

The honest reading is narrower than 4.0% suggests. Kyverno unit-test failures
come in two well-formed shapes with unambiguous markers, while the Podman
sample spans BATS/TAP, Ginkgo, PowerShell, installers, 504s and size gates.
This shows the taxonomy generalizes to structured Go tooling output, the easy
case, not that it generalizes broadly. The 25 jobs also collapse to roughly 8
distinct root causes, since the same broken commit was retried across runs.

Zero environment-caused failures appeared, so that workflow is the wrong place
to hunt flakes. Kyverno's conformance suite is a reusable workflow invoked via
`workflow_call`, so it reports no runs of its own and its jobs are attributed
to the caller.
