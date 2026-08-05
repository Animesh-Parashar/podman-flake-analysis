# Podman CI flake detection and analysis

A prototype of the ingestion, detection and analysis stages described in
[cncf/mentoring#1963](https://github.com/cncf/mentoring/issues/1963).

Built and measured against real `podman-container-tools/podman` CI data, to
establish what the failures actually look like before deciding how to classify
them. Standard library only, no third-party dependencies, reproducible against
the public Actions API with a token.

Two of the results here argue against the obvious implementation, and one
headline finding was retracted after a contamination check caught it. Both are
written up below.

## Stages

| Stage | Script | What it does |
|---|---|---|
| 1. Collect | `collect_testruns.py` | streams job logs, extracts per-test TAP outcomes, discards the logs |
| 2. Detect | `rank_flakes.py` | aggregates outcomes across runs, applies contamination checks C1-C8, ranks candidates |
| 3. Classify | `flake_analyzer.py` | rule taxonomy over a single failed job, with evidence and test ids |
| 4. Triage | `llm_triage.py` | puts a local model on the failures the rules could not name |
| Validate | `validate_against_issues.py` | ranking versus maintainer-labelled `flakes` issues, with a negative control |
| Validate | `c7_control.py` | bounds the failed-run sampling bias |

Stages 1 and 2 answer "which tests are flaky", which is a cross-run property.
Stages 3 and 4 answer "why did this job fail", which is a per-log property.
They are separate questions and the code keeps them separate.

## Quick start

```bash
gh auth login          # or export GITHUB_TOKEN

# classify a small sample of failures
python3 flake_analyzer.py --limit 25 --json-out failures.json

# detect flakes across many runs
python3 collect_testruns.py --limit 441 --status failure --workers 16 \
        --out fail_runs.jsonl -v
python3 rank_flakes.py --in fail_runs.jsonl --top 20 --min-shas 5 \
        --min-branches 3 --json-out ranking.json
```

Full reproduction, roughly two hours of wall time:

```bash
python3 collect_testruns.py --limit 120 --status success --workers 12 \
        --out success_runs.jsonl -v
python3 validate_against_issues.py --ranking ranking.json \
        --issues bodies.json --top 20 --thresh 0.5
python3 c7_control.py --failed fail_runs.jsonl --success success_runs.jsonl \
        --total-failed 441 --total-success 662
python3 llm_triage.py --failures failures.json --model qwen2.5:32b \
        --control 20 --out triage.json
```

`bodies.json` is the maintainer-labelled flake corpus and is not checked in.
Regenerate it with:

```bash
gh issue list --repo podman-container-tools/podman --label flakes --state all \
   --limit 400 --json number,title,body > bodies.json
```

Use `--state all`. Restricting to open issues drops 89% of the corpus, which is
the mistake described under validation below.

`collect_testruns.py` streams and discards logs rather than caching them.
Podman job logs run to roughly 22 MB each; the full sample streams 938 MB.
`flake_analyzer.py` does cache, under `.cache/` (gitignored), because iterating
on the taxonomy against fixed logs should be free.

`llm_triage.py` talks to [Ollama](https://ollama.com) on `localhost:11434`.
Local rather than a hosted model on purpose: a flake-triage tool that ships CI
logs to a third party is a harder thing for maintainers to adopt than one that
does not.

Every script takes `--repo` and defaults to `podman-container-tools/podman`.

## Scale

Window 2026-06-01 to 2026-08-03, 62 days.

| | Failed-run sample | Success control |
|---|---:|---:|
| runs | 441 | 120 |
| jobs enumerated (`^sys ` filter) | 3,792 | 1,560 |
| logs retrieved | 3,660 | 988 |
| logs purged by retention (410) | 132 (3.5%) | 572 (37%) |
| **per-test observations** | **2,896,278** | 792,532 |
| raw failure events | 381 | 0 |
| distinct commits | 284 | 75 |
| distinct branches | 80 | |
| API calls | 4,238 | 1,682 |

Podman ran 1,103 `ci.yml` runs in that window, 441 of which failed, so the
failed-run sample is complete rather than sampled.

Walking only failed runs is what makes this affordable. Podman's system tests
are BATS, which emits TAP, and TAP prints `ok N <name>` for passes as well as
`not ok N <name>` for failures. One failed job's log therefore carries the full
pass/fail vector for every test in that job. Ginkgo does not print passes, so
this technique does not extend to the `int` suites without a different signal.

## Detection

Signals, in the priority the literature gives them:

1. **flip rate**, the strongest of the three features in the published F1 95.5%
   classifier. A test that both passes and fails is the canonical signature.
2. **same-commit flips**, passed and failed on the identical commit. The
   strongest evidence obtainable without reruns, because the code under test is
   held constant by construction.
3. **spread**, distinct branches and commits failed on.
4. **mode differential**, parallel versus serial. Podman runs the same tests
   both ways and the TAP prefix records which (`|NNN|` parallel, `[NNN]`
   serial), so the comparison is free here.

Counts are per distinct commit, never per raw event.

### Contamination checks

Reported as C1-C8 in the output. Six were written down before any data was
collected; C7 was designed alongside the collection method it bounds; C8 was
added after it invalidated a result.

| | Check | Result on this sample |
|---|---|---|
| C1 | one broken commit retried across runs | 1.01 runs per commit, negligible |
| C2 | one spec failing in root/rootless/remote of one run | dominant, see below |
| C3 | a real regression, time-concentrated, not a flake | 0 flagged |
| C4 | log retention bias (410s) | 3.5% loss over 62 days |
| C5 | one test splitting into several ids | 962 ids, 828 base names |
| C6 | one long-lived branch pushed many times | 80 branches / 284 commits |
| C7 | failed-run sampling bias | 3.33x median overstatement |
| C8 | a test failing only where someone is editing it | 76 of 93 tests excluded |

**C1 and C2 together remove 40.7% of raw failure events.** 381 raw events
collapse to 226 distinct `(commit, test)` pairs, because one spec failing
across the 13 `sys` job variants is one event, not thirteen. C1 turns out to be
negligible and C2 accounts for nearly all of it.

**C7 is arithmetic, not a per-test measurement.** The denominator is "runs in
which something failed", not "all runs", so every flip rate here is an upper
bound. `(284 + 662) / 284 = 3.33`. Relative ranking is unaffected, since the
bias applies to every test in the same jobs. Absolute claims of the form "test
X is flaky 12% of the time" are not supported.

**C8 is the guard that matters most.** 76 of the 93 tests with at least one
failure fail on fewer than three branches and are excluded.

### Ranked candidates

`--min-shas 5 --min-branches 3`, 17 of 91 survive. Top five:

| flip | fail/seen commits | same-commit | branches | test |
|---:|---:|---:|---:|---|
| 0.099 | 28/284 | **28** | **13** | `\|220\| podman healthcheck` |
| 0.039 | 11/284 | 11 | 6 | `\|610\| check Go template formatting` |
| 0.028 | 8/283 | 8 | 4 | `\|252\| quadlet - image tag` |
| 0.028 | 8/284 | 8 | 4 | `\|280\| podman update - set ulimits` |
| 0.025 | 7/284 | 7 | 4 | `\|055\| podman container rm --force doesn't leave running processes` |

The top entry passed and failed on all 28 of the same commits, across 13
branches including `main`, `v5.8` and `v6.0`. All five fail on `main`, which is
the discriminator: a developer's work in progress cannot cause a failure there.

### A retracted result

The mode differential produced the best-looking finding in the project.
`podman healthcheck - corrupted log file is handled gracefully` failed 10 of 38
times under parallel execution and 0 of 1,564 under serial, Fisher exact
p = 1.6e-17.

All ten failures came from one branch, `fix/healthcheck-log-corruption`, where
somebody was editing that exact test. Three of the four significant
mode-differential results were single-branch artifacts of the same kind.

C3, the pre-registered regression check, does not catch this. A
work-in-progress branch is neither a flake nor a regression in the mainline
sense; it is somebody's broken intermediate state. Catching it required a new
check asking whether the failing spec was itself being edited on the branch it
failed on, which is C8. **After the guard, one comparable test remains and it
is not significant. The parallel versus serial question is unanswered.**

## Validation against maintainer labels

Podman maintainers label known-flaky issues `flakes`. If a ranking derived
purely from CI outcome history surfaces tests maintainers independently filed
issues about, that is evidence the detector measures what they mean by "flake".

**The pre-registered test failed.** Two matches in the top 20 against 1.99
expected by chance from 200 random draws. Exactly chance, zero signal. The
pre-registered threshold was three.

Two things were wrong with it, both found afterwards:

- **The corpus was 11% of the real one.** 42 open issues, when Podman has 376
  `flakes`-labelled issues, 334 of them closed. A closed flake issue is still a
  maintainer judgement about what counts as a flake, arguably a better one
  since it was triaged and resolved.
- **The matcher was directional and broken.** It computed
  `|issue ∩ test| / |issue|`, so a short test name (`|220| podman healthcheck`
  reduces to one token after stopwords) could never reach the threshold against
  a long issue title.

| Analysis | Corpus | Matcher | Matches | Chance | Verdict |
|---|---|---|---:|---:|---|
| **Pre-registered** | 42 open | bag-of-words | 2/20 | 2.00 | exactly chance |
| Revised corpus | 376 | bag-of-words | 3/20 | 3.00 | exactly chance |
| Revised corpus and matcher | 376 | targeted substring | 8/17 | 4.98 | 1.61x, p=0.093 |

**Both revisions are post-hoc and the hypothesis remains unconfirmed.** The
direction is positive and the effect is 1.6x, but p=0.093 does not clear
significance at n=17, and the chance rate is high (29%) because 376 issues
cover much of Podman's test surface.

Two of the top four ranked tests turned out to be known flakes the registered
test could not see: `|280| podman update - set ulimits` is
[#28940](https://github.com/podman-container-tools/podman/issues/28940), closed
and therefore outside an open-only corpus, and `|220| podman healthcheck` is
[#29353](https://github.com/podman-container-tools/podman/issues/29353), filed
with no `flakes` label at all.

Independently of the statistics: **21 of the 42 issues concern harnesses this
collection never observed** (11 Ginkgo, 6 machine, 4 unclear). The hypothesis
was tested for the `sys` subset and is untested elsewhere.

## Classification

`flake_analyzer.py` assigns a category to a single failed job from its log.
Four behaviours were only discoverable by running against real logs, and each
one breaks a naive implementation.

**`make: *** [Makefile:705: ginkgo] Error 2` is a symptom, not a cause.**
Podman runs its test suites through make targets (`ginkgo`, `localsystem`,
`localmachine`), so matching that line as a build failure mislabels test
failures. In an intermediate version that combined a wide context window with a
`make:` rule, it drove `build_artifact` to 70.5% of the sample. It now sits in
`GENERIC_ERRORS` alongside `Process completed with exit code N`: terminal
markers that report that something failed without reporting what.

**The failure line can sit well above the `##[error]` marker.** BATS emits a
long run of `ok N ...` lines, then the make error at the end. Measured across
the sample, the `not ok N` explaining the failure sits **48 to 943 lines above
the marker, median 186.** That is outside any practical context window, so the
extractor also runs a position-independent whole-log scan for structured
failure lines (`not ok`, `--- FAIL:`, Ginkgo `[FAILED]`). An agent whose
context window is anchored on the error marker reads the symptom and misses the
cause.

**Context has to be asymmetric.** Most jobs end with a generic marker and the
real cause is above it, hence 30 lines back versus 4 forward, with a preference
for anchoring on specific annotations wherever any exist.

**Aggregator jobs double-count.** The `Total Success` gate job fails whenever
any other job fails. It accounted for 25 of 120 failed jobs, 20.8% of the raw
sample. Excluded by default.

PowerShell and Ginkgo also colourise output, and the ANSI escapes sit between
the words the patterns need to match, so regions are stripped before matching.

Same 95 failed jobs, initial rule set versus current:

| | initial | current |
|---|---:|---:|
| unclassified | 85.3% | 5.3% |

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `--repo` | `podman-container-tools/podman` | target repository |
| `--workflow` | `ci.yml` | workflow file to sample |
| `--limit` | 60 | failed runs to ingest |
| `--exclude-jobs` | `^(Total Success\|...)$` | aggregator/gate jobs to drop |
| `--out` | `flake-report.md` | markdown report |
| `--json-out` | none | `{failures: [...], rerun_confirmed: [...]}`, also the input to `llm_triage.py` |

### Sample findings

25 failed `ci.yml` runs, 95 failed jobs, July 2026. This is the classifier's
own sample and is separate from the 441-run detection sample above.

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

### Out-of-sample check

The taxonomy was developed against the Podman sample, so its 5.3% unclassified
rate is measured on the data it was fitted to. The unmodified analyzer was
pointed at `kyverno/kyverno` (`check-unit-tests.yaml`, 25 failed jobs), a Go
repository with a different test harness, as a holdout.

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

### Evidence capture: a defect found by trying to use the tool

The `evidence` field was anchored on the line that triggered the taxonomy
match. For Ginkgo that is the bare marker, because `test_assertion`'s first
pattern is `\[FAILED\]` and Ginkgo prints `[FAILED] Expected` at the failure
point with the spec name several lines away in its `Summarizing N Failures:`
block. **22 different failures in the sample collapsed to that one identical
string.**

The metric is stated over the **114 jobs of 126 where a taxonomy rule
matched**. The other 12 are `unclassified` or `log_unavailable`: no rule
matched, or the log was purged by retention, so there is no triggering line to
quote and no test name to find. Those cannot carry evidence by construction and
counting them would flatter the result.

| Over 114 classified jobs | Before | After |
|---|---:|---:|
| Evidence that is empty or a bare marker | 35 (30.7%) | **0 (0.0%)** |
| Jobs carrying a test name | 0 | **91 (79.8%)** |
| `[FAILED] Expected` occurrences | 22 | **0** |
| Distinct evidence strings (all 126 jobs) | 69 | **90** |

This is the same root cause as the TAP-distance finding above, which had been
applied to classification and never to evidence capture. The extraction window
already contained the spec name; `classify()` discarded it.

The fix adds an `IDENTIFYING` pass that pulls test names from Ginkgo
(`[FAIL] <suite> [It] <spec>`), `go test` (`--- FAIL: TestName`) and TAP/BATS
(`not ok N <name>`), exposed as a `test_ids` field. Category selection is
untouched, and **category assignment is identical for all 126 jobs**, verified
by diffing before and after.

Verified cold, with the log cache removed: 0 hits / 37 fetches, 22 jobs, zero
bare markers, and 0 of 10 classified jobs uninformative. The warm-cache and
cold-cache results agree.

Re-running the known-flake match with the improved evidence moved it from 1/126
to 3/126. Inspecting all three: two are a correct exact match on
[#28868](https://github.com/podman-container-tools/podman/issues/28868), and
one is a false positive matching `error: 404` against `error: 504`, because
token-overlap matching discards the numeric literal that carries the meaning.
So the true match rate is 2/126, and a known-flake dictionary is not a useful
triage filter on this sample.

The change is still worth having. Per-test aggregation was impossible while
every Ginkgo failure shared one evidence string.

## LLM triage

The `unclassified` bucket is the interface to a model stage. Those are the
failures every regex declined, so they are the exact queue worth spending
inference on, and the rules provide no answer to grade against, which forces
hand-checking.

Two sets are run. **Target** is the jobs the rules left `unclassified`; no
ground truth exists, so every answer is read against its log by hand.
**Control** is jobs the rules classified confidently, used only to check the
model agrees on easy cases. If it does not, nothing it says about the target
set is worth reading.

The model is not asked to judge flakiness. Flakiness is a cross-run property
and a model reading one log cannot see the other 283 commits. It is asked to
categorise a root cause from text, which is a language task it is suited to.

| Control set, 20 jobs | qwen2.5:7b | qwen2.5:32b |
|---|---:|---:|
| agreement with the rules | 9/20 = 45% | **19/20 = 95%** |

Fisher exact p = 1.25e-03. The 7B model fails the pre-registered 70% floor, so
its target answers carry no weight; the 32B passes.

**Every error both models made ran in the same direction.** Collapsed to the
axis that matters, environment-caused versus genuine defect:

| Over the hand-checked target set | 7b | 32b |
|---|---:|---:|
| correct environment calls | 7 | 7 |
| **real bug called environment** | **2** | **3** |
| **environment failure called a bug** | **0** | **0** |

Not once did either model make the reverse mistake. For a flake-triage tool
that is the dangerous direction, because it marks real defects as
infrastructure noise, which is precisely the failure this project exists to
prevent. A deterministic layer deciding what the model is allowed to conclude,
and a human reading the output, is the obvious response.

The model did find causes the rules missed, including an apt failure
(`E: Failed to fetch ... Hashes of expected file`) that the
`dependency_install` pattern cannot catch, because it requires a package
manager name on the same line and apt's format does not provide one.

**An automated groundedness scorer was written and then discarded.** It scored
keyword overlap between the model's stated reason and the log, and reported the
opposite of the truth: generic tokens score as grounded (`actions` matches
`actions/upload-artifact` in every log) while the 32B's specific terms were
mangled by the tokenizer. Hand-checking is the only method reported here.

## What this is not

- **Not an agent.** One model call per failure, no tools, no retrieval, no
  iteration.
- **Not a Ginkgo flake detector.** Ginkgo does not emit passes, so flip rate is
  not computable for the `int` suites from logs alone.
- **Not a source of absolute flake rates.** See C7.
- **Not validated against maintainer labels.** The hypothesis is unconfirmed,
  not confirmed.

## Limitations

- The `^sys ` job filter is deliberate and is also the largest gap. 21 of 42
  labelled flake issues concern harnesses never observed here.
- Only 2 rerun-confirmed flakes appeared in the classifier sample, both
  `Validate source code changes`. Reruns are the only zero-heuristic flake
  signal available from the Actions API and they are rare, which is why the
  cross-run detector exists at all.
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
  reported as `log_unavailable`. The success control lost 37% of its logs this
  way against 3.5% for the failed sample, because the successful runs sampled
  were older.
- Windows job logs contain wide-character spacing (`F o r  m o r e`) that
  defeats naive matching. Not yet normalized.
- The taxonomy was developed against its own sample, so the 5.3% unclassified
  figure is fitted. The Kyverno holdout is one narrow out-of-sample point.

## Prior art

| Method | Idea | Transfers? |
|---|---|---|
| Rerun / pass-fail-pass (Google) | pattern-match execution logs | partly, around 100 reruns needed to surface half the flakes |
| [DeFlaker](https://www.cs.cornell.edu/~legunsen/pubs/BellETAL18DeFlaker.pdf), ICSE 2018 | newly-failing test that did not execute changed code | no, needs coverage instrumentation |
| iDFlakies, ICST 2019 | randomised test order | no, needs control of ordering |
| [Meta PFS](https://engineering.fb.com/2020/12/10/developer-tools/probabilistic-flakiness/) | per-test probabilistic flakiness score | yes, in spirit |
| [FlakeFlagger](https://www.jonbell.net/preprint/icse21-flakeflagger.pdf), [Flakify](https://arxiv.org/pdf/2112.12331), [practical prediction](https://arxiv.org/pdf/2302.09330) | F1 95.5% from three features, flip rate strongest | yes, primary signal |
| Chromium Flake Portal | per-test history across the fleet | yes, in shape |

## License

[Apache 2.0](LICENSE).
