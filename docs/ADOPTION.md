# Adoption baseline

sessionFlow does not collect product telemetry. Adoption is measured only through public/aggregate repository signals and voluntarily submitted feedback.

This page records the launch baseline. It is not a user census: stars, clones and downloads are noisy proxies, and no signal here proves that a person understood or used the product.

## Baseline before Core v1.6.1

Collected from GitHub API on 2026-11-26 (local baseline verification at 2026-11-25T07:14:12Z).

| Signal | Core `HarryHeYu/sessionFlow` | Plugin `HarryHeYu/dsh-sessionflow` | Notes |
| --- | ---: | ---: | --- |
| Stars | 7 | 3 | Public, cumulative. |
| Forks | 1 | 1 | Public, cumulative. |
| Contributors | not separately snapshotted | not separately snapshotted | Add at first post-launch review. |
| Latest published release | `v1.6.0` | `v1.4.0` | Before R19 tag. |
| Release asset downloads | wheel 2, sdist 0 | tgz 3 | Cumulative per asset at query time. |
| Repository clones (GitHub traffic, last 14 days) | 3316 total / 420 unique | 740 total / 217 unique | Public to repository owners only; “unique” is GitHub’s count, not confirmed people. |
| Repository views (GitHub traffic, last 14 days) | 84 total / 49 unique | 22 total / 10 unique | Aggregate page traffic. |
| Open issues | 4 | 1 | Core issues are all owner-authored. Plugin #2 is from an external user. |
| Open PRs | 0 | 0 | R18 PRs merged. |
| External continuation-quality reports | 0 | 0 | No structured feedback yet. |
| Community post responses | not started | not started | The R19 launch plan has not been executed yet. |

## What will be reviewed after launch

At day 7 and day 30, record:

- stars, forks and contributors;
- release asset downloads for `v1.6.1`;
- clone/view traffic over the preceding 14 days;
- issues from external users, split by bug / provider / continuation quality / feature request;
- demo-related questions, even when they arrive outside GitHub;
- responses on whichever single channel was actually used that day;
- whether any external user completed the five-minute demo and reported what broke.

## Interpretation rule

```text
A star is not an adoption.
A clone is not an installation.
An installation is not a continuation.
A continuation report is the first real adoption signal.
```

The launch success criterion is deliberately modest: 3–10 people who genuinely switch coding agents try the five-minute demo and tell us where it fails.
