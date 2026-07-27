# CI failure analysis: `podman-container-tools/podman` / `ci.yml`

Sample: **95 failed jobs** across 25 failed workflow runs.

## Root-cause distribution

| Category | Jobs | Share | Meaning |
|---|---:|---:|---|
| `test_assertion` | 65 | 68.4% | Test assertion failure, most likely a genuine defect |
| `timing_race` | 13 | 13.7% | Timing-dependent failure: deadline, race detector, or wait loop |
| `unclassified` | 5 | 5.3% | No taxonomy rule matched, candidate for a new rule |
| `size_regression` | 4 | 4.2% | Binary size gate: build grew past the allowed delta |
| `build_artifact` | 3 | 3.2% | Build, packaging, or artifact upload failure |
| `lint_static` | 2 | 2.1% | Linter or static analysis violation |
| `network_timeout` | 2 | 2.1% | Network unreachable, DNS failure, or transport timeout |
| `github_infra` | 1 | 1.1% | GitHub's own infrastructure throttled or failed the job |

**Environment-caused (infra/network/registry/dependency/timing): 15/95 = 15.8%**. These are flakes rather than defects, and are the addressable target.

## Failures on `main`

19 failed jobs landed on the default branch. `main` should be green, so each is either a real regression or a flake, making this the highest-value queue to triage.

| Date | Job | Category | Evidence |
|---|---|---|---|
| 2026-07-24 | [macos installer](https://github.com/podman-container-tools/podman/actions/runs/30107532084/job/89528519000) | `build_artifact` | `##[error]No files were found with the provided path: podman-remote-release-darwin_arm64.zi` |
| 2026-07-24 | [windows installer hyperv](https://github.com/podman-container-tools/podman/actions/runs/30107532084/job/89528519106) | `network_timeout` | `curl: (22) The requested URL returned error: 504` |
| 2026-07-24 | [build fedora-rawhide / lima](https://github.com/podman-container-tools/podman/actions/runs/30107532084/job/89528519365) | `github_infra` | `##[warning]Failed to download action 'https://codeload.github.com/actions/checkout/tar.gz/` |
| 2026-07-24 | [windows installer wsl](https://github.com/podman-container-tools/podman/actions/runs/30107532084/job/89528519503) | `network_timeout` | `\| 504 Gateway Time-out The server didn't respond in time.` |
| 2026-07-24 | [macos machine libkrun](https://github.com/podman-container-tools/podman/actions/runs/30091954903/job/89478955107) | `timing_race` | `[FAILED] Timed out after 30.002s.` |
| 2026-07-24 | [macos machine libkrun](https://github.com/podman-container-tools/podman/actions/runs/30077783788/job/89433671431) | `test_assertion` | `[FAILED] Expected` |
| 2026-07-24 | [sys local rootless fedora-rawhide / lima](https://github.com/podman-container-tools/podman/actions/runs/30077783788/job/89433672247) | `test_assertion` | `not ok 129 \|065\| podman cp dir from container to host in 4197ms` |
| 2026-07-24 | [sys local root debian-sid / lima](https://github.com/podman-container-tools/podman/actions/runs/30077783788/job/89433672650) | `test_assertion` | `not ok 300 [500] podman network create in 986ms` |
| 2026-07-23 | [windows machine hyperv](https://github.com/podman-container-tools/podman/actions/runs/30020921850/job/89254953294) | `test_assertion` | `[FAILED] Expected` |
| 2026-07-23 | [macos machine applehv](https://github.com/podman-container-tools/podman/actions/runs/30020921850/job/89254953318) | `timing_race` | `[FAILED] Timed out after 600.002s.` |
| 2026-07-23 | [int remote root debian-sid / lima](https://github.com/podman-container-tools/podman/actions/runs/29991122831/job/89155042712) | `test_assertion` | `[FAILED] Command failed with exit status 125. See above for error message.` |
| 2026-07-23 | [sys local root fedora-rawhide / lima](https://github.com/podman-container-tools/podman/actions/runs/29978086160/job/89114923340) | `test_assertion` | `not ok 43 \|030\| podman run no /etc/mtab in 9257ms` |
| 2026-07-22 | [sys local root fedora-rawhide / lima](https://github.com/podman-container-tools/podman/actions/runs/29903310568/job/88870952548) | `test_assertion` | `not ok 461 \|700\| podman play with image volume pull policies in 3176ms` |
| 2026-07-22 | [sys local root debian-sid / lima](https://github.com/podman-container-tools/podman/actions/runs/29903310568/job/88870952690) | `unclassified` | `` |
| 2026-07-22 | [int local rootless fedora-rawhide / lima](https://github.com/podman-container-tools/podman/actions/runs/29903310568/job/88870952707) | `unclassified` | `` |

## Most frequently failing jobs

| Job | Failures | Categories seen |
|---|---:|---|
| `int local rootless fedora-rawhide / lima` | 7 | test_assertion×5, timing_race×1, unclassified×1 |
| `Validate source code changes` | 6 | size_regression×4, lint_static×2 |
| `macos machine libkrun` | 5 | timing_race×4, test_assertion×1 |
| `int local rootless fedora-current / lima` | 5 | test_assertion×4, timing_race×1 |
| `windows machine hyperv` | 4 | test_assertion×4 |
| `macos machine applehv` | 4 | timing_race×4 |
| `int local rootless debian-sid / lima` | 4 | test_assertion×3, timing_race×1 |
| `sys local root debian-sid / lima` | 4 | test_assertion×3, unclassified×1 |
| `sys local root fedora-rawhide / lima` | 4 | test_assertion×4 |
| `int local rootless fedora-prior / lima` | 4 | test_assertion×3, timing_race×1 |
| `sys local rootless fedora-rawhide / lima` | 4 | test_assertion×4 |
| `int local root fedora-rawhide / lima` | 4 | test_assertion×4 |
| `sys local rootless fedora-current / lima` | 3 | test_assertion×3 |
| `int remote rootless fedora-current / lima` | 3 | test_assertion×2, timing_race×1 |
| `int remote root debian-sid / lima` | 3 | test_assertion×2, unclassified×1 |

## Rerun-confirmed flakes

2 job(s) failed on one attempt and passed on the latest attempt of the same run. This is ground truth: no heuristics, no log parsing, and the natural seed set for evaluating any classifier.

| Run | Job |
|---|---|
| [30076927369](https://github.com/podman-container-tools/podman/actions/runs/30076927369) | `Validate source code changes` |
| [30004632979](https://github.com/podman-container-tools/podman/actions/runs/30004632979) | `Validate source code changes` |

## Unclassified

5 job(s) matched no rule. In the full system these are the queue an LLM stage should triage, with its output reviewed and folded back into the taxonomy.

- [bud remote root fedora-current / lima](https://github.com/podman-container-tools/podman/actions/runs/30076927369/job/89433448043), steps: Run test on lima
- [bud local root fedora-current / lima](https://github.com/podman-container-tools/podman/actions/runs/30076927369/job/89433448157), steps: Run test on lima
- [sys local root debian-sid / lima](https://github.com/podman-container-tools/podman/actions/runs/29903310568/job/88870952690), steps: Run lima-vm/lima-actions/setup@55627e31b78637bf254a8b2a14da8ea7d12564e5
- [int local rootless fedora-rawhide / lima](https://github.com/podman-container-tools/podman/actions/runs/29903310568/job/88870952707), steps: Run lima-vm/lima-actions/setup@55627e31b78637bf254a8b2a14da8ea7d12564e5
- [int remote root debian-sid / lima](https://github.com/podman-container-tools/podman/actions/runs/29903310568/job/88870952815), steps: Run lima-vm/lima-actions/setup@55627e31b78637bf254a8b2a14da8ea7d12564e5
