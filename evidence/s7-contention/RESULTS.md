# S7 executed contention receipt

Production source installed: `33fd5ac997b1fe56c3496540ab1a1dfcf5c3c892` (noneditable wheel). Benchmark implementation: `f34a61ae`; subsequent declaration-derived policy list selects the identical two cases.

Command:

```sh
.venv/bin/python benchmarks/profile_headless.py --scratch /home/ts/wt/comms-s7-contention-20260928/.scratch --output evidence/s7-contention/current.jsonl --seconds 180
```

Three real spawned poller processes, 20 seed + 200 measured channel sends per case, current private bus and claim barrier enabled. All six cases completed; all 1,200 measured sends persisted exactly once (220 durable rows in each fresh case). Native production candidate maintenance remained enabled. No provider or live-root operation.

## Current send latency

| Registered threads | Read policy | Send p50 ms | Send p99 ms | Failed polls / attempts | Scratch MiB |
|---:|---|---:|---:|---:|---:|
| 50 | CurrentSharedReads | 71.22 | 114.36 | 0/890 | 5.31 |
| 50 | ForcedExclusiveReads | 77.16 | 111.04 | 0/964 | 5.31 |
| 100 | ForcedExclusiveReads | 136.68 | 193.33 | 1/1443 | 9.97 |
| 100 | CurrentSharedReads | 147.48 | 229.15 | 2/1215 | 9.97 |
| 150 | CurrentSharedReads | 178.40 | 245.60 | 0/1678 | 14.63 |
| 150 | ForcedExclusiveReads | 180.64 | 280.52 | 2/1764 | 14.63 |

## Registry read lock timing

Ranges below are the three poller processes individually, not percentiles of pooled percentile values. Full per-process/store distributions (p50, p99, maximum, total, count) are in `current.jsonl`.

| Threads | Read policy | Raw flock wait p99 ms range | Acquisition including barrier p99 ms range | Hold including barrier p99 ms range |
|---:|---|---:|---:|---:|
| 50 | CurrentSharedReads | 0.0064–0.0069 | 0.2112–0.2272 | 1.2432–1.2755 |
| 50 | ForcedExclusiveReads | 1.1842–1.6039 | 1.3670–1.6990 | 1.2477–1.2860 |
| 100 | ForcedExclusiveReads | 1.7863–2.1478 | 1.8400–2.2382 | 1.3262–1.3508 |
| 100 | CurrentSharedReads | 0.0075–0.0095 | 0.2424–0.2705 | 1.3062–1.3565 |
| 150 | CurrentSharedReads | 0.0077–0.0085 | 0.2379–0.2574 | 1.2034–1.2742 |
| 150 | ForcedExclusiveReads | 1.3278–1.8285 | 1.4129–1.8858 | 1.3112–1.3625 |

## Sender bus lock timing

Main-thread bus acquisitions only; native maintenance and other stores are separately labelled in the JSON receipt. Bus acquisition includes the real private durability barrier.

| Threads | Read policy | Raw flock wait p50 / p99 ms | Full acquisition p50 / p99 ms | Hold p50 / p99 ms |
|---:|---|---:|---:|---:|
| 50 | CurrentSharedReads | 26.194 / 40.899 | 30.661 / 47.945 | 43.388 / 77.595 |
| 50 | ForcedExclusiveReads | 27.612 / 43.714 | 33.318 / 47.734 | 47.002 / 81.451 |
| 100 | ForcedExclusiveReads | 64.082 / 104.563 | 72.180 / 112.004 | 66.690 / 109.488 |
| 100 | CurrentSharedReads | 68.241 / 143.786 | 76.200 / 152.508 | 73.157 / 134.569 |
| 150 | CurrentSharedReads | 104.973 / 138.059 | 110.680 / 145.689 | 70.774 / 123.709 |
| 150 | ForcedExclusiveReads | 109.419 / 157.553 | 116.365 / 162.955 | 69.101 / 141.737 |

## Interpretation and concrete finding

Shared reads reduce registry syscall wait clearly, but this run does not show a uniform send-latency improvement. The 100-thread shared send tail is slower, while the 150-thread shared tail is faster. The host was concurrently loaded; these are one-run observations, not confidence intervals or a historical speedup claim.

**Production reader finding:** five failed polls in the complete matrix raised `RelationViolationError: Private registry guard marker is not owner-only` (`RegistryStore.private_guard_unlocked`, registry_store.py:127). It happened with both current shared and forced-exclusive reads. Initial fail-fast execution also caught this error and is retained in `first-attempt.log`; that interrupted case is not presented as passing. The observed exception is verified; its root cause is not diagnosed by this benchmark. Production lock/guard repair belongs to the parent integration owner, outside this benchmark-only change.

Peak recorded RSS of any benchmark process: **422.9 MiB**; configured address-space ceiling 768 MiB per process. Maximum scratch: **14.63 MiB**, versus 128 MiB configured. Poller worker count remained three. All disposable case roots were removed in finally, including the failed initial case.

Lock samples retain real flock and durable barriers. Raw wait includes syscall overhead. Full acquisition includes open/path overhead and the barrier. Hold starts after flock and ends just after context exit/close, including small measurement overhead. Failed polls are included in poll-attempt duration and counted separately; they are never described as successful reads.

## Recovered pre-step-1 source measurement

Unmodified installed source `9cb6651fa07d3613587ecf10db53d038c3d63a98`, parent of the first A8 LockedStore commit. Historical private/claim entrypoints, historical real exclusive locks, same three-poller workload and 200 measured sends per size. All 600 measured sends persisted (220 rows including seeds per case); no failed poll. Exact recovery instructions and differences: `HISTORICAL.md`. This is not an A8-only causal comparison.

| Threads | Send p50 ms | Send p99 ms | Poll attempts | Registry raw wait p99 ms range | Registry hold p99 ms range |
|---:|---:|---:|---:|---:|---:|
| 50 | 172.32 | 312.55 | 600 | 0.010–0.013 | 0.625–0.752 |
| 100 | 306.55 | 506.98 | 601 | 0.008–0.010 | 0.651–0.678 |
| 150 | 424.48 | 757.91 | 600 | 0.011–0.014 | 0.714–0.772 |

| Threads | Sender bus raw wait p50 / p99 ms | Full acquisition p50 / p99 ms | Hold p50 / p99 ms |
|---:|---:|---:|---:|
| 50 | 51.757 / 120.810 | 65.313 / 143.171 | 113.919 / 196.443 |
| 100 | 111.669 / 207.814 | 134.194 / 245.680 | 190.799 / 344.498 |
| 150 | 147.451 / 256.261 | 189.378 / 337.266 | 275.309 / 511.764 |

Historical peak process RSS **437.1 MiB**; maximum case scratch **11.37 MiB**. Historical scratch is empty after all three cases. The recovery-only checkout/environment and current disposable environment were removed after copying receipts; the current source worktree remains.

## Scope and deletion closure

Deleted the obsolete serial listing/transcript benchmark, removed Comms facade calls and retired ACP replay manipulation from the maintained benchmark. No compatibility dispatch, replay alternative or production-lock change was added. Benchmark source diff: 374 added / 141 deleted; growth implements the missing three-process workload, actual lock timing, resource bounds and failure accounting. The historical translation exists only as a receipt patch, not as an active compatibility path.

No benchmark acceptance measurement remains unexecuted. Production marker-reader failures are still a finding for the parent integration owner; this receipt does not call the runtime error-free. CI was not used as a gate.
