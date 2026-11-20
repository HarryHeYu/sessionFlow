# Runner isolation gate — 2026-11-20

> Verdict: **BLOCKED**. A live Agent runner must not be enabled in the Level 2 benchmark. The current harness is DRY-RUN only; no model, paid call or GUI Agent was launched.

## 1. What was checked

The host is Windows, user `He_Yu_Hao`, Git Bash uid `197611`. Read-only capability probes:

| Probe | Result |
|---|---|
| `docker` | not found |
| `podman` | not found |
| `unshare`, `bwrap`, `runuser`, `setpriv`, `sandbox-exec` | not found |
| `wsl.exe` | present; distro `Ubuntu_New` present |
| WSL identity | `uid=1000(heyuhao) gid=1000(heyuhao) groups=1000(heyuhao),27(sudo)` |
| WSL view of host scratch | readable |
| WSL view of `C:\Users\He_Yu_Hao` | directory visible |
| WSL unprivileged process reading `baseline_a/marker.txt`, `baseline_b/marker.txt`, `hidden/marker.txt` | all three exit code `0` |

The marker files were synthetic and lived only in `E:/sessionflow-scratch/r4-v2-tests/isolation-probe/`. No real session, home content, credential or SSH material was read.

## 2. Option comparison

| Option | Available here? | Boundary evidence | Verdict |
|---|---|---|---|
| Independent OS user | No safe way to create one; `reg.exe` is policy-blocked and no `useradd`/`runuser` exists. | Current Windows user and WSL uid 1000 both map to the same host profile. | BLOCKED |
| Restricted container | `docker`/`podman` absent; no container runtime config supplied. | No per-baseline mount/deny policy can be asserted. | BLOCKED |
| Separate VM | WSL is present but default-drive automount exposes host files. | Unprivileged WSL process read both baseline markers and hidden marker. | BLOCKED as configured |
| Remote isolated executor | No authorized endpoint, credential or network budget. | No evidence collected. | BLOCKED |
| Shell-level env cleaning only | Present in `runner_contract()`. | Clears env and isolates paths by convention, but cannot stop traversal to sibling repos or real index. | Not a security boundary |

## 3. Why this blocks live execution

`runner_contract()` can describe a clean `cwd`/env, and private-path string checks can catch obvious mistakes, but neither prevents an agent process from reading:

- sibling baseline repos under the same workdir;
- `private/hidden_tests/test_hidden.py`;
- `private/task_manifest.json`;
- real HOME or provider token inherited through another channel.

The WSL probe is decisive: an unprivileged uid could read all synthetic markers. This is an inspectable contract, not an enforced boundary.

## 4. Conditions before enabling a live runner

All conditions must hold and be recorded with the result:

1. An OS-level sandbox creates one isolated execution identity per baseline run.
2. A no-permission probe inside that identity fails to read sibling baseline repos, hidden tests, task manifest, real HOME, provider tokens and SSH agent.
3. The grader and hidden tests are outside every runner-writable tree.
4. Baseline repos, home directories and indexes are mutually unreadable.
5. CPU, memory, wall-clock and process count limits are enforced; timeout force-terminates the whole sandbox.
6. Network access is denied unless separately authorized.
7. Result rows bind model id/version/parameters, environment hash, runner image and cost account.
8. Model and spend are explicitly authorized; default remains `DRY_RUN` / `LIVE_MODEL_NOT_VERIFIED`.

Until then, `classify_outcome()` may be tested with synthetic verdicts, but no completion rate may be reported.
