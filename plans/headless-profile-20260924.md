# Headless thread and transcript profile (2026-09-24)

Measured on Linux with Python 3.11, isolated `/var/tmp` wires, no Pi or provider.
The benchmark created synthetic threads, 500 bus messages, and Pi JSONL session
files; setup was excluded from read timings. Results are medians of 7 to 9 warm
calls or 3 cold calls, except a single cold transcript call. The benchmark
script is [benchmarks/profile_headless.py](../benchmarks/profile_headless.py);
raw JSON from the final implementation is retained at
`/var/tmp/ac-headless-profile-final.jsonl` on this machine.

## Thread listing

| Threads | Bus messages | Before PR #1 warm | Merged PR #1 warm | Merged PR #1 cold |
| ---: | ---: | ---: | ---: | ---: |
| 25 | 500 | 2,268 ms | 8.8 ms | 9.3 ms |
| 100 | 500 | — | 25.5 ms | 27.1 ms |
| 400 | 500 | — | 93.5 ms | 97.5 ms |

The before-PR #1 measurement used `aa18e45`; the merged result used the
byte-identical PR #1 tree plus CI-only fixes. The 25-thread comparison is a
roughly 250-fold improvement. The current listing pass scales with threads
and bus rows, with one pending-count scan rather than one scan per thread.

## Transcript history

With no routing annotations, a 20-event tail page stayed near 0.3 ms warm
from 1,000 through 100,000 JSONL rows (0.17 MB to 17 MB). A cold 100,000-row
page took 0.9 ms and allocated 0.13 MB. Headless ACP snapshot replay of that
history took about 1.1 ms.

The original global `transcript_routes.json` map was a separate growth cost.
At 100,000 transcript rows and 100,000 routing entries, a cold page took
631 ms and allocated 41.8 MB; a warm page took 2.8 ms. The indexed route store
in this branch returns the same 20 events with these measurements:

| Routing entries | Cold page | Cold allocation | Warm page | ACP snapshot replay | One route update |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 100,000 | 1.6 ms | 0.13 MB | 0.65 ms | 1.68 ms | 0.22 ms |

The new store imports the legacy JSON map in one SQLite transaction, then
reads only the entry IDs on each page. If an older process keeps writing JSON
during a rolling upgrade, new file revisions are imported without overwriting
routes written by the new store. The live wire has a 26 MB legacy map;
it was inspected read-only and was **not** migrated or modified during this
profile. Its first migration will take time proportional to that file size.
Subsequent transcript reads use the indexed database.
