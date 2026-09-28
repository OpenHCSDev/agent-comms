# PR289 ready: registry marker contention fix

Source implementation: `dab7cd54e7307017ccd0d3e60c27a049dfe757d7`; merged current main/PR285 in `dbde81cca189435de6d54d24de2bf010b939a7ed` (benchmark and receipts only). Installed package was built from the fixed production source, noneditable; the benchmark receipt records the site-packages import path.

## Verified cause and fix

The unmodified installed main reproducer recorded actual failed stat values: uid 1000 (the running owner), mode 0600, nlink=0 during canonical atomic marker replacement. `red.log` retains that failure. The original S7 PR285 red receipts are unchanged in `evidence/s7-contention/`.

WireLog synchronizes its canonical marker read/publication using the canonical leaf store lock. The same opened inode is decoded and validated under that lock. RegistryStore deletes its independent racing lstat. Owner uid, exact 0600 mode, regular-file and exactly-one-link checks remain strict; symlinks are refused by no-follow open. Atomic replacement, file fsync, parent-directory fsync and the existing bus durability barrier all remain.

Lock graph: caller bus/registry transaction -> marker leaf. `_store_lock(bus_meta.json)` calls WireLog.verify_before_read_unlocked, whose claim_gate_enabled returns immediately for every filename except bus.jsonl. Thus the marker leaf never acquires bus or registry locks. There is no inverse order introduced.

## Installed acceptance

- Actual 1,500 canonical marker publications versus three independent registry readers: **44,395 reads, zero failures**. Together with existing checkpoint regressions: **26 passed in 16.95s** (`green.log`).
- Real on-disk 0644 mode, extra hardlink and symlink each still refuse registry reads, canonical marker reads and sends; no bus append occurs: **3 passed in 0.61s** (`policy.log`).
- S7 rerun complete: **all six cases**, 50/100/150 threads, three actual pollers each, **1,200 measured sends durable**, **7,646 polls, zero failures**. Each fresh case also retains all 20 seed messages. No guard substitution or failure exclusion. Results below; raw per-process/store lock metrics are in `s7-fixed.jsonl`.

| Threads | Read policy | Send p50 ms | Send p99 ms | Polls | Failed polls |
|---:|---|---:|---:|---:|---:|
| 50 | CurrentSharedReads | 86.44 | 146.45 | 984 | 0 |
| 50 | ForcedExclusiveReads | 84.35 | 134.92 | 1024 | 0 |
| 100 | ForcedExclusiveReads | 131.79 | 211.58 | 1181 | 0 |
| 100 | CurrentSharedReads | 145.89 | 215.74 | 1179 | 0 |
| 150 | CurrentSharedReads | 200.36 | 294.23 | 1636 | 0 |
| 150 | ForcedExclusiveReads | 188.66 | 279.88 | 1642 | 0 |

Command:

```sh
.venv/bin/python /home/ts/wt/comms-s7-contention-20260928/benchmarks/profile_headless.py --scratch /home/ts/wt/comms-registry-marker-contention-20260928/.scratch --output evidence/registry-marker-contention/s7-fixed.jsonl --seconds 180
```

Peak benchmark process RSS: 442.7 MiB; maximum case scratch: 14.63 MiB. Three polling workers, default 768 MiB/process and 128 MiB/case budgets. Every case scratch root was removed in finally. Owned test roots and environment cleaned after receipts were committed.

## Integration

Ready for parent merge/install. Participating readers/writers need the installed fix so both sides honor the marker leaf lock; no schema or durable-history migration is introduced. No remaining diagnosed source/caller blocker in this scope. No live-root writes, message replay or provider calls were used. CI deferred.

Production diff: 51 lines added / 32 deleted across existing canonical owners; the added code establishes the missing synchronized read/publication boundary. One actual concurrent regression plus a three-member physical ownership-policy family added (105 test lines). No compatibility path or alternate store.
