# Live Core lock census — 2026-10-16

Owner: `codex-shepherd`. Read-only measurement for `claude-code`; no product-code changes, application input, worker restart or runtime installation.

## Result

The largest measured queues are on the root wire barrier, registry and bus. The wire barrier is carried into registry waits; registry JSON decoding and repeated sidecar integrity checks extend the chain. Optimizing each caller independently would preserve the cause.

**This is a physical-resource census with a source-site inventory, not exact per-acquisition timing.** The requested tools cannot produce true hold-time p50/p95 or completely attribute every process-owned flock to one Python thread. Those values are unknown, not zero. The table reports observation counts and sample spans instead; it does not claim the missing per-site measurements were obtained.

### Top five by sampled kernel waiter-seconds (estimates)

1. **`.wire.lock` — 1078.2 waiter-s**, maximum 50 queued requests at a snapshot; **B; A for pure observers**. `agent_activity.py:249 observe_native_phase; queued_input.py:434 DeliveredInput._recover_locked; goal_management.py:328 Goals.goal_snapshot`.
2. **`.registry.json.lock` — 692.3 waiter-s**, maximum 36 queued requests at a snapshot; **A reads / B writes**. `locked_store.py:162 LockedStore.reading; registry_store.py:130 RegistryStore.read_entry; registry_store.py:149 RegistryStore.editing`.
3. **`.bus.jsonl.lock` — 204.2 waiter-s**, maximum 20 queued requests at a snapshot; **A reads / B preparation / C append**. `wire_log.py:293 WireLog.locked; wire_log.py:302 WireLog.certified_read; wire_log.py:383 WireLog.record_context`.
4. **`.native_prompt_bindings.sqlite3.snapshot-lock` — 60.5 waiter-s**, maximum 7 queued requests at a snapshot; **B; A for read-only use**. `private_sidecar.py:136 _locked_directory; private_sidecar.py:349 sidecar_connection`.
5. **`.bus_meta.json.lock` — 59.3 waiter-s**, maximum 18 queued requests at a snapshot; **A read / C durable write**. `wire_log.py:780 WireLog.read_metadata_unlocked; wire_log.py:824 WireLog.write_metadata_unlocked`.

Classes: **A** read work can consume a correctly certified snapshot; **B** compute/decode/preparation should move out of the serialized commit scope, with required revision checks; **C** the write or single-owner lifetime really needs exclusion; **D** two independent authorities write the same fact. Mixed resources have separate read/write cases. **No D classification is established by this measurement**; multiple writer processes alone do not prove duplicate semantic ownership.

## Window, coverage and limits

- Window: 2026-10-16T16:35:41.654520+00:00 through 2026-10-16T16:45:41.654837+00:00; **600 snapshots in 600.000 seconds**.
- Target cadence 1 Hz; actual lslocks interval p50 **1.000s**, p95 **1.250s**, maximum **1.813s**. Early scheduling lag reached **13.586s** while the collector was mistakenly pinned to two CPUs; only its own affinity was then widened. Catch-up samples are retained, not relabeled perfectly uniform sampling.
- 99 distinct selected PIDs over the window: 21 worker PIDs, 72 other Core/ACP/CLI PIDs and six Toad PIDs. Selection uses package-declared console entrypoints/module ownership and the live root, plus Toad processes without an explicit alternate root. Includes worker/ACP/bridge/short-lived CLI activity and six Toad PIDs; it is not 99 simultaneous workers.
- `lslocks 2.42.4`; native `py-spy 0.4.2 dump --json --full-filenames --nonblocking`, no locals: **15505/16773 successful dumps**. Nonblocking races caused 1178 failures, including `Failed to copy PyCodeObject; Bad address (os error 14)`; other startup/exit errors remain in raw data. Existing default Toad PID 3238085: 556 successful / 600 attempted dumps. No failed dump was silently treated as a successful empty stack.
- lslocks command duration p50/p95 **0.381/0.925s**; py-spy dump duration **0.102/0.232s**. File-lock and stack snapshots are not simultaneous.
- Observer overhead: 44.7 collector CPU-s plus 1177.7 child CPU-s over 600s, approximately 2.04 CPU cores; collector peak RSS 33.7 MiB. This is observed live workload under that profiling overhead, not an unperturbed benchmark. Resource check reported critical swap/disk pressure; no agents, application tests, provider calls or generated workload were launched.
- Waiter-seconds = sum of observed queued kernel lock requests times the interval to the next snapshot. Estimates can over/undershoot short bursts. Counts can exceed wall time because several threads wait concurrently. SQLite uses nonblocking lock attempts/busy handlers, so **zero kernel waiters does not mean zero SQLite contention**. Async and threading locks do not appear in lslocks.
- A holder observation is one non-waiting lslocks row. A run is adjacent observations of the same PID/device/inode/mode/range/FD-holder tuple. Its span is last minus first observation; **0 means one observation, not zero-duration holding**. Release/reacquire between points is invisible; lifecycle runs can be left/right censored. These spans are NOT lower bounds on a continuous hold.
- Truncated `/...` names were resolved only from a unique device/inode-to-path association actually seen elsewhere in the same measurement. Unresolved identities remain separate rows. Paths, not inferred names, identify resources.
- Holder-stack lists below require a source-compatible executing scope in a sampled holder process. They remain candidate correlations: PID-owned locks plus separate snapshots do not generally prove one holding thread. An acquisition frame for the same sidecar is excluded. Unmatched/ambiguous cases are counted, not assigned by guesswork.
- This cannot enumerate locks acquired and released entirely between samples, suspended asyncio tasks, every C-library mutex, or all dynamic aliases. Exact per-call hold quantiles and complete per-site waiter attribution would require additional duration tracing, not more confident labels on these samples.

## Source basis

The live fleet is mixed-version. Stack paths include Core `50f2b0e00` and `8099d1f6e` in four immutable runtime prefixes, plus `/current` aliases. A small amount of naturally occurring source-only CLI checking was also visible; it was not induced as workload. Aliased filenames cannot prove in-memory module bytes. Modules containing direct file-lock/SQL-BEGIN primitives compared equal across the four installed roots except `cli_bridges/shared.py`; the inventory retains root and source hashes.

Reused refactor-audit `Package.load` through a filesystem `Repository` specialization: **1312 module parses**, **0 parse omissions**, **551 syntactic candidate sites**. These include 15 direct file-lock calls, 23 explicit SQL-BEGIN calls and 27 mutex/semaphore declarations; other sites are contexts, delegated acquisitions and escaped ExitStack lifetimes. Potential connection contexts include implicit SQLite transactions; opening a writer connection alone does not prove a write transaction. Dynamic receiver binding is not fully resolved.

`store_files._store_lock` opens the named `.NAME.lock`, delegates to `PosixPlatform.acquire_store_lock` (`child_process.py:232`) or the nonblocking path (`:238`), verifies the bus source when appropriate, and releases by descriptor close. `LockedStore` is the document owner; it is not a second lock implementation.

**Journal modes are not uniform.** Read-only 100-byte SQLite file headers after the window show rollback format (1/1) for `coordination.sqlite3`, `bus_page_index.sqlite3`, `private_bus_checkpoint.sqlite3`, `transcript_routes.sqlite3`, `transcript_reply_index.v4.sqlite3`, `native_prompt_bindings.sqlite3` and `goal_history.sqlite3`; WAL (2/2) for `input_dispositions.sqlite3`, `wake_candidates.sqlite3`, `external_transcripts.sqlite3` and `bus_route_counts.v3.sqlite3`. No SQLite connections were opened for this check. Do not describe all readers as WAL readers or simply replace BEGIN EXCLUSIVE with BEGIN IMMEDIATE: the current coordinator intentionally acquires exclusion before changing state to avoid commit-time busy uncertainty.

## One table: measured resources and explicit acquisition owners

Names are relative to the live root unless absolute. Source references are relative to installed `site-packages/agent_comms` (the corresponding repository files are `src/agent_comms`). Resource rows aggregate callers; explicit-owner rows below them are source-only unless a resource match is shown. They are not additive counts of independent lock acquisitions. Exact hold p50/p95 is **unknown for every row**.

| Resource / acquisition site | Owner function and work while held | Class | Holder observations / sample runs | Sample-span p50 / p95 (s), NOT hold duration | Max kernel waiters / estimated waiter-s | Stack evidence |
|---|---|---|---:|---:|---:|---|
| `.wire.lock` | `agent_activity.py:249 observe_native_phase; queued_input.py:434 DeliveredInput._recover_locked; goal_management.py:328 Goals.goal_snapshot`<br>Shared/exclusive root barrier: registry snapshots/phase writes, activity append, input recovery and SQL queries; waits for nested registry/bus locks; fsync via mutations. No network/child work demonstrated in these captured global-lock stacks. | B; A for pure observers | 376 / 333 | 0.000 / 0.000 | 50 / 1078.2 | [S1](#s1); 263 unmatched, 26 ambiguous |
| `.registry.json.lock` | `locked_store.py:162 LockedStore.reading; registry_store.py:130 RegistryStore.read_entry; registry_store.py:149 RegistryStore.editing`<br>Read/stat/guard checks, whole JSON decode (even single-entry cold reads), document copy, mutation, encode; temp-file and directory fsync on writes. Nested marker-lock wait observed. No direct network/child in these methods. | A reads / B writes | 672 / 626 | 0.000 / 0.000 | 36 / 692.3 | [S2](#s2); 601 unmatched, 1 ambiguous |
| `.bus.jsonl.lock` | `wire_log.py:293 WireLog.locked; wire_log.py:302 WireLog.certified_read; wire_log.py:383 WireLog.record_context`<br>Source certificate/marker reads, bounded source capture and SQL projections; context-record encoding, append/checkpoint work and fsync; nested marker wait. Preserve fixed-prefix/source checks; no network/child demonstrated. | A reads / B preparation / C append | 591 / 558 | 0.000 / 0.000 | 20 / 204.2 | [S3](#s3); 504 unmatched, 3 ambiguous |
| `.native_prompt_bindings.sqlite3.snapshot-lock` | `private_sidecar.py:136 _locked_directory; private_sidecar.py:349 sidecar_connection`<br>Exclusive even for reads: read bytes, deserialize into RAM SQLite, full schema/digest/quick_check on entry AND exit, caller SQL, optional serialize/guarded replacement and fsync. No network/child in this owner; callback scope itself is generic. | B; A for read-only use | 205 / 177 | 0.000 / 1.001 | 7 / 60.5 | [S4](#s4); 114 unmatched, 0 ambiguous |
| `.bus_meta.json.lock` | `wire_log.py:780 WireLog.read_metadata_unlocked; wire_log.py:824 WireLog.write_metadata_unlocked`<br>Opened-inode safety checks, JSON/FieldCodec decode and round-trip canonicality check; encode, replace and file+directory fsync for writes. No network/child. | A read / C durable write | 175 / 166 | 0.000 / 0.000 | 18 / 59.3 | [S5](#s5); 166 unmatched, 0 ambiguous |
| `.input_dispositions.sqlite3.lock` | `typed_table.py:782 TypedStore._attached; typed_table.py:787 retire; store_files.py:216 _store_lock`<br>Serialize WAL connection attachment/retirement, including schema reads/checks and possible initialization. Normal admitted WAL queries run after this file lock is released. | B attachment / C lifecycle boundary | 13 / 12 | 0.000 / 0.000 | 9 / 8.7 | [S6](#s6); 13 unmatched, 0 ambiguous |
| `.read_ledger.json.lock` | `store_files.py:216 _store_lock; locked_store.py:162 reading / :170 update / :184 replace`<br>Shared snapshot read/decode, or read/transform/encode and atomic replacement with file+directory fsync. Arbitrary update callback can extend scope; no per-caller claim from this resource-only observation. | A read / B update | 41 / 37 | 0.000 / 0.000 | 1 / 6.1 | [S7](#s7); 41 unmatched, 0 ambiguous |
| `.runtime_info.json.lock` | `store_files.py:216 _store_lock; locked_store.py:162 reading / :170 update / :184 replace`<br>Shared snapshot read/decode, or read/transform/encode and atomic replacement with file+directory fsync. Arbitrary update callback can extend scope; no per-caller claim from this resource-only observation. | A read / B update | 1 / 1 | 0.000 / 0.000 | 1 / 1.3 | [S8](#s8); 1 unmatched, 0 ambiguous |
| `/home/ts/.local/state/agent-comms` | `active_route.py:214 guard_default_route_write`<br>Default-route read/check then whole borrowed client/worker lifetime; can include child/network activity. Shared route fence prevents changing root beneath a live writer, not a data-writer mutex. | C lifecycle | 18349 / 4668 | 0.000 / 11.981 | 0 / 0.0 | [S9](#s9); 18349 unmatched, 0 ambiguous |
| `native-session/.2026-10-14T13-54-51-327Z_01a13ab1-c540-7168-ae58-7ac34c3a093a.jsonl.agent-comms-writer.lock` | `session_fence.py:24 _try_lock; session_fence.py:52 session_writer_fence`<br>One writer per native session through one executor operation, potentially including native/provider network work. Long lifetime is intentional; no queued kernel waiters observed. | C lifecycle | 590 / 38 | 9.443 / 39.968 | 0 / 0.0 | [S10](#s10); 590 unmatched, 0 ambiguous |
| `coordination.sqlite3` | `coordination_session.py:51 CoordinationSession.transaction; coordination_database.py:95 begin_snapshot`<br>SQLite-managed byte locks: reads/row codecs or caller DML and commit/checkpoint fsync depending store/mode. Resource-level family association, not per-sample transaction attribution. SQLite busy-handler waiters are NOT counted by lslocks. | A reader / B preparation / C SQL write | 1374 / 1254 | 0.000 / 0.000 | 0 / 0.0 | [S11](#s11); 1374 unmatched, 0 ambiguous |
| `bus_route_counts.v3.sqlite3` | `bus_route_counts.py:424 BusRouteCounts.sync; :353 _initialize`<br>SQLite-managed byte locks: reads/row codecs or caller DML and commit/checkpoint fsync depending store/mode. Resource-level family association, not per-sample transaction attribution. SQLite busy-handler waiters are NOT counted by lslocks. | A reader / B preparation / C SQL write | 119 / 114 | 0.000 / 0.000 | 0 / 0.0 | [S12](#s12); 119 unmatched, 0 ambiguous |
| `bus_route_counts.v3.sqlite3-shm` | `bus_route_counts.py:424 BusRouteCounts.sync; :353 _initialize`<br>SQLite-managed byte locks: reads/row codecs or caller DML and commit/checkpoint fsync depending store/mode. Resource-level family association, not per-sample transaction attribution. SQLite busy-handler waiters are NOT counted by lslocks. | A reader / B preparation / C SQL write | 221 / 209 | 0.000 / 0.000 | 0 / 0.0 | [S13](#s13); 221 unmatched, 0 ambiguous |
| `bus_page_index.sqlite3` | `bus_page_index.py:141 BusPageIndex._sync_opened; :81 __init__`<br>SQLite-managed byte locks: reads/row codecs or caller DML and commit/checkpoint fsync depending store/mode. Resource-level family association, not per-sample transaction attribution. SQLite busy-handler waiters are NOT counted by lslocks. | A reader / B preparation / C SQL write | 53 / 39 | 0.000 / 1.640 | 0 / 0.0 | [S14](#s14); 53 unmatched, 0 ambiguous |
| `input_dispositions.sqlite3` | `typed_table.py:843 TypedStore.writing; :810 _open_writer; coordination_database.py:95 begin_snapshot`<br>SQLite-managed byte locks: reads/row codecs or caller DML and commit/checkpoint fsync depending store/mode. Resource-level family association, not per-sample transaction attribution. SQLite busy-handler waiters are NOT counted by lslocks. | A reader / B preparation / C SQL write | 38 / 37 | 0.000 / 0.000 | 0 / 0.0 | [S15](#s15); 38 unmatched, 0 ambiguous |
| `input_dispositions.sqlite3-shm` | `typed_table.py:843 TypedStore.writing; :810 _open_writer; coordination_database.py:95 begin_snapshot`<br>SQLite-managed byte locks: reads/row codecs or caller DML and commit/checkpoint fsync depending store/mode. Resource-level family association, not per-sample transaction attribution. SQLite busy-handler waiters are NOT counted by lslocks. | A reader / B preparation / C SQL write | 66 / 66 | 0.000 / 0.000 | 0 / 0.0 | [S16](#s16); 66 unmatched, 0 ambiguous |
| `.activity_latest.json.lock` | `store_files.py:216 _store_lock; locked_store.py:162 reading / :170 update / :184 replace`<br>Shared snapshot read/decode, or read/transform/encode and atomic replacement with file+directory fsync. Arbitrary update callback can extend scope; no per-caller claim from this resource-only observation. | A read / B update | 12 / 11 | 0.000 / 0.000 | 0 / 0.0 | [S17](#s17); 12 unmatched, 0 ambiguous |
| `owner-restart-queue/watcher.lock` | `restart_queue.py:284 run`<br>Watch/await, queue reads, worker restart work and nested queue lock for one watcher lifetime. Child-process work can occur; not a global data-write lock. | C singleton | 615 / 9 | 45.985 / 221.970 | 0 / 0.0 | [S18](#s18); 615 unmatched, 0 ambiguous |
| `/home/ts/.local/state/claude-comms-inbox/watch.lock` | `cli_bridges/shared.py:512 Inbox.run`<br>Entire bridge watch/task lifetime: registry presence, Core watch transport/child and delivery-journal updates. Nonblocking single-instance guard. | C singleton | 579 / 45 | 6.074 / 37.980 | 0 / 0.0 | [S19](#s19); 579 unmatched, 0 ambiguous |
| `owner-restart-queue/.queue.lock` | `store_files.py:216 _store_lock; locked_store.py:162 reading / :170 update / :184 replace`<br>Shared snapshot read/decode, or read/transform/encode and atomic replacement with file+directory fsync. Arbitrary update callback can extend scope; no per-caller claim from this resource-only observation. | A read / B update | 118 / 93 | 0.000 / 1.989 | 0 / 0.0 | [S20](#s20); 118 unmatched, 0 ambiguous |
| `private_bus_checkpoint.sqlite3` | `private_bus_checkpoint.py:1510 append_private_bus_checkpoint_unlocked (implicit db transaction); :1188 read snapshot`<br>SQLite-managed byte locks: reads/row codecs or caller DML and commit/checkpoint fsync depending store/mode. Resource-level family association, not per-sample transaction attribution. SQLite busy-handler waiters are NOT counted by lslocks. | A reader / B preparation / C SQL write | 381 / 366 | 0.000 / 0.000 | 0 / 0.0 | [S21](#s21); 381 unmatched, 0 ambiguous |
| `.catalog.json.lock` | `store_files.py:216 _store_lock; locked_store.py:162 reading / :170 update / :184 replace`<br>Shared snapshot read/decode, or read/transform/encode and atomic replacement with file+directory fsync. Arbitrary update callback can extend scope; no per-caller claim from this resource-only observation. | A read / B update | 5 / 5 | 0.000 / 0.000 | 0 / 0.0 | [S22](#s22); 5 unmatched, 0 ambiguous |
| `wake_candidates.sqlite3` | `typed_table.py:843 TypedStore.writing; :810 _open_writer; coordination_database.py:95 begin_snapshot`<br>SQLite-managed byte locks: reads/row codecs or caller DML and commit/checkpoint fsync depending store/mode. Resource-level family association, not per-sample transaction attribution. SQLite busy-handler waiters are NOT counted by lslocks. | A reader / B preparation / C SQL write | 11 / 11 | 0.000 / 0.000 | 0 / 0.0 | [S23](#s23); 11 unmatched, 0 ambiguous |
| `wake_candidates.sqlite3-shm` | `typed_table.py:843 TypedStore.writing; :810 _open_writer; coordination_database.py:95 begin_snapshot`<br>SQLite-managed byte locks: reads/row codecs or caller DML and commit/checkpoint fsync depending store/mode. Resource-level family association, not per-sample transaction attribution. SQLite busy-handler waiters are NOT counted by lslocks. | A reader / B preparation / C SQL write | 25 / 25 | 0.000 / 0.000 | 0 / 0.0 | [S24](#s24); 25 unmatched, 0 ambiguous |
| `/home...` | `store_files.py:216 _store_lock; locked_store.py:162 reading / :170 update / :184 replace`<br>Shared snapshot read/decode, or read/transform/encode and atomic replacement with file+directory fsync. Arbitrary update callback can extend scope; no per-caller claim from this resource-only observation. | A read / B update | 6 / 6 | 0.000 / 0.000 | 0 / 0.0 | [S25](#s25); 6 unmatched, 0 ambiguous |
| `.goal_waits.json.lock` | `store_files.py:216 _store_lock; locked_store.py:162 reading / :170 update / :184 replace`<br>Shared snapshot read/decode, or read/transform/encode and atomic replacement with file+directory fsync. Arbitrary update callback can extend scope; no per-caller claim from this resource-only observation. | A read / B update | 15 / 15 | 0.000 / 0.000 | 0 / 0.0 | [S26](#s26); 15 unmatched, 0 ambiguous |
| `<unresolved 8:35:3940215>` | `No source owner assigned: device/inode unresolved`<br>Kernel POSIX rows observed, but the pathname and transaction owner are unknown. Do not infer a database or classify the work from that alone. | unknown | 3 / 3 | 0.000 / 0.000 | 0 / 0.0 | [S27](#s27); 3 unmatched, 0 ambiguous |
| `goal-private/goal_attempts.sqlite3` | `goal_attempts.py:634 GoalAttemptStore._change; :570 initialize`<br>SQLite-managed byte locks: reads/row codecs or caller DML and commit/checkpoint fsync depending store/mode. Resource-level family association, not per-sample transaction attribution. SQLite busy-handler waiters are NOT counted by lslocks. | A reader / B preparation / C SQL write | 3 / 3 | 0.000 / 0.000 | 0 / 0.0 | [S28](#s28); 3 unmatched, 0 ambiguous |
| `.activity.jsonl.lock` | `store_files.py:216 _store_lock; locked_store.py:162 reading / :170 update / :184 replace`<br>Shared snapshot read/decode, or read/transform/encode and atomic replacement with file+directory fsync. Arbitrary update callback can extend scope; no per-caller claim from this resource-only observation. | A read / B update | 8 / 7 | 0.000 / 1.017 | 0 / 0.0 | [S29](#s29); 8 unmatched, 0 ambiguous |
| `.external_transcripts.sqlite3.lock` | `typed_table.py:782 TypedStore._attached; typed_table.py:787 retire; store_files.py:216 _store_lock`<br>Serialize WAL connection attachment/retirement, including schema reads/checks and possible initialization. Normal admitted WAL queries run after this file lock is released. | B attachment / C lifecycle boundary | 2 / 2 | 0.000 / 0.000 | 0 / 0.0 | [S30](#s30); 2 unmatched, 0 ambiguous |
| `external_transcripts.sqlite3` | `typed_table.py:843 TypedStore.writing; :810 _open_writer; coordination_database.py:95 begin_snapshot`<br>SQLite-managed byte locks: reads/row codecs or caller DML and commit/checkpoint fsync depending store/mode. Resource-level family association, not per-sample transaction attribution. SQLite busy-handler waiters are NOT counted by lslocks. | A reader / B preparation / C SQL write | 2 / 2 | 0.000 / 0.000 | 0 / 0.0 | [S31](#s31); 2 unmatched, 0 ambiguous |
| `external_transcripts.sqlite3-shm` | `typed_table.py:843 TypedStore.writing; :810 _open_writer; coordination_database.py:95 begin_snapshot`<br>SQLite-managed byte locks: reads/row codecs or caller DML and commit/checkpoint fsync depending store/mode. Resource-level family association, not per-sample transaction attribution. SQLite busy-handler waiters are NOT counted by lslocks. | A reader / B preparation / C SQL write | 3 / 3 | 0.000 / 0.000 | 0 / 0.0 | [S32](#s32); 3 unmatched, 0 ambiguous |
| `.transcript_routes.sqlite3.lock` | `transcript_routes.py:155 _ensure_database; :285 record_input_display; :188 record`<br>File exclusion includes SQL transaction: schema checks/possible creation, existing-route comparison, row encoding/insertion and commit/fsync. Not merely a connection attachment lock. No network/child in these methods. | C write / B schema preparation | 3 / 3 | 0.000 / 0.000 | 0 / 0.0 | [S33](#s33); 3 unmatched, 0 ambiguous |
| `transcript_routes.sqlite3` | `transcript_routes.py:292 record_input_display; :193 record (implicit transaction)`<br>SQLite-managed byte locks: reads/row codecs or caller DML and commit/checkpoint fsync depending store/mode. Resource-level family association, not per-sample transaction attribution. SQLite busy-handler waiters are NOT counted by lslocks. | A reader / B preparation / C SQL write | 6 / 6 | 0.000 / 0.000 | 0 / 0.0 | [S34](#s34); 6 unmatched, 0 ambiguous |
| `<unresolved 8:35:4459281>` | `No source owner assigned: device/inode unresolved`<br>Kernel POSIX rows observed, but the pathname and transaction owner are unknown. Do not infer a database or classify the work from that alone. | unknown | 1 / 1 | 0.000 / 0.000 | 0 / 0.0 | [S35](#s35); 1 unmatched, 0 ambiguous |
| `<unresolved 8:35:3940267>` | `No source owner assigned: device/inode unresolved`<br>Kernel POSIX rows observed, but the pathname and transaction owner are unknown. Do not infer a database or classify the work from that alone. | unknown | 4 / 4 | 0.000 / 0.000 | 0 / 0.0 | [S36](#s36); 4 unmatched, 0 ambiguous |
| `.bus_route_counts.v3.sqlite3.lock` | `bus_route_counts.py:419 BusRouteCounts.sync; :350 _initialize`<br>Source delta decode is already before this file lock. Under it: open SQLite, compare recorded revision, insert/delete prepared rows and commit/fsync. Initialization also installs/checks schema. No network/child in this owner. | C bounded commit | 3 / 3 | 0.000 / 0.000 | 0 / 0.0 | [S37](#s37); 3 unmatched, 0 ambiguous |
| `<unresolved 8:35:3940249>` | `No source owner assigned: device/inode unresolved`<br>Kernel POSIX rows observed, but the pathname and transaction owner are unknown. Do not infer a database or classify the work from that alone. | unknown | 4 / 4 | 0.000 / 0.000 | 0 / 0.0 | [S38](#s38); 4 unmatched, 0 ambiguous |
| `native-session/.2026-10-16T16-41-22-297Z_01a14596-f07b-7588-b36d-d9c240abebaa.jsonl.agent-comms-writer.lock` | `session_fence.py:24 _try_lock; session_fence.py:52 session_writer_fence`<br>One writer per native session through one executor operation, potentially including native/provider network work. Long lifetime is intentional; no queued kernel waiters observed. | C lifecycle | 83 / 6 | 2.065 / 59.013 | 0 / 0.0 | [S39](#s39); 83 unmatched, 0 ambiguous |
| `.wake_candidates.sqlite3.lock` | `typed_table.py:782 TypedStore._attached; typed_table.py:787 retire; store_files.py:216 _store_lock`<br>Serialize WAL connection attachment/retirement, including schema reads/checks and possible initialization. Normal admitted WAL queries run after this file lock is released. | B attachment / C lifecycle boundary | 5 / 5 | 0.000 / 0.000 | 0 / 0.0 | [S40](#s40); 5 unmatched, 0 ambiguous |
| `<unresolved 8:35:3940272>` | `No source owner assigned: device/inode unresolved`<br>Kernel POSIX rows observed, but the pathname and transaction owner are unknown. Do not infer a database or classify the work from that alone. | unknown | 2 / 2 | 0.000 / 0.000 | 0 / 0.0 | [S41](#s41); 2 unmatched, 0 ambiguous |
| `.owner_release_receipts.json.lock` | `store_files.py:216 _store_lock; locked_store.py:162 reading / :170 update / :184 replace`<br>Shared snapshot read/decode, or read/transform/encode and atomic replacement with file+directory fsync. Arbitrary update callback can extend scope; no per-caller claim from this resource-only observation. | A read / B update | 1 / 1 | 0.000 / 0.000 | 0 / 0.0 | [S42](#s42); 1 unmatched, 0 ambiguous |
| `<unresolved 8:35:3940264>` | `No source owner assigned: device/inode unresolved`<br>Kernel POSIX rows observed, but the pathname and transaction owner are unknown. Do not infer a database or classify the work from that alone. | unknown | 2 / 2 | 0.000 / 0.000 | 0 / 0.0 | [S43](#s43); 2 unmatched, 0 ambiguous |
| `<unresolved 8:35:4458243>` | `No source owner assigned: device/inode unresolved`<br>Kernel POSIX rows observed, but the pathname and transaction owner are unknown. Do not infer a database or classify the work from that alone. | unknown | 1 / 1 | 0.000 / 0.000 | 0 / 0.0 | [S44](#s44); 1 unmatched, 0 ambiguous |
| `<unresolved 8:35:3940190>` | `No source owner assigned: device/inode unresolved`<br>Kernel POSIX rows observed, but the pathname and transaction owner are unknown. Do not infer a database or classify the work from that alone. | unknown | 4 / 4 | 0.000 / 0.000 | 0 / 0.0 | [S45](#s45); 4 unmatched, 0 ambiguous |
| `acp.py:276` (memory-declaration) | `CommsAgent._prompt_request` — `asyncio.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `active_route.py:214` (primitive-file) | `guard_default_route_write` — `fcntl.flock(directory, fcntl.LOCK_SH \| (0 if blocking else fcntl.LOCK_NB))`<br>Default-route read/check then whole borrowed client/worker lifetime; can include child/network activity. Shared route fence prevents changing root beneath a live writer, not a data-writer mutex. | C exclusion; consumer-dependent work | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `active_route.py:274` (primitive-file) | `acquire_exclusive_route_lock` — `fcntl.flock(directory, fcntl.LOCK_EX \| fcntl.LOCK_NB)`<br>Default-route read/check then whole borrowed client/worker lifetime; can include child/network activity. Shared route fence prevents changing root beneath a live writer, not a data-writer mutex. | C exclusion; consumer-dependent work | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `active_route.py:304` (primitive-file) | `publish_active_route` — `fcntl.flock(directory, fcntl.LOCK_EX)`<br>Default-route read/check then whole borrowed client/worker lifetime; can include child/network activity. Shared route fence prevents changing root beneath a live writer, not a data-writer mutex. | C exclusion; consumer-dependent work | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `activity.py:310` (memory-declaration) | `ActivityReadState` — `field(default_factory=threading.Lock)`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `bus_page_index.py:81` (primitive-sql) | `BusPageIndex.__init__` — `self.connection.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `bus_page_index.py:140` (scope) | `BusPageIndex._sync_opened` — `self.connection`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `bus_page_index.py:141` (primitive-sql) | `BusPageIndex._sync_opened` — `self.connection.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `bus_route_counts.py:353` (primitive-sql) | `BusRouteCounts._initialize` — `connection.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `bus_route_counts.py:424` (primitive-sql) | `BusRouteCounts.sync` — `connection.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `candidate_maintenance.py:21` (memory-declaration) | `` — `threading.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `channel_coding_tools.py:94` (memory-declaration) | `CodingToolOwner` — `field(default_factory=asyncio.Lock, repr=False, compare=False)`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `child_process.py:232` (primitive-file) | `PosixPlatform.acquire_store_lock` — `fcntl.flock(descriptor, mode \| (0 if blocking else fcntl.LOCK_NB))`<br>Physical acquisition only; caller (_store_lock/async driver/LockedStore) owns body, reads, decode, SQL and fsync. Win32 branch is not executed on this Linux host. | C exclusion; consumer-dependent work | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `child_process.py:238` (primitive-file) | `PosixPlatform.try_store_lock` — `fcntl.flock(descriptor, mode \| fcntl.LOCK_NB)`<br>Physical acquisition only; caller (_store_lock/async driver/LockedStore) owns body, reads, decode, SQL and fsync. Win32 branch is not executed on this Linux host. | C exclusion; consumer-dependent work | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `child_process.py:403` (primitive-file) | `Win32Platform.try_store_lock` — `msvcrt.locking(descriptor, msvcrt.LK_NBRLCK if shared else msvcrt.LK_NBLCK, 1)`<br>Physical acquisition only; caller (_store_lock/async driver/LockedStore) owns body, reads, decode, SQL and fsync. Win32 branch is not executed on this Linux host. | C exclusion; consumer-dependent work | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `cli_bridges/shared.py:361` (scope) | `DeliveryJournal.bind` — `self.db`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `cli_bridges/shared.py:365` (primitive-sql) | `DeliveryJournal.bind` — `self.db.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `cli_bridges/shared.py:371` (scope) | `DeliveryJournal.bind` — `self.db`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `cli_bridges/shared.py:375` (primitive-sql) | `DeliveryJournal.bind` — `self.db.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `cli_bridges/shared.py:378` (scope) | `DeliveryJournal.begin` — `self.db`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `cli_bridges/shared.py:382` (scope) | `DeliveryJournal.finish` — `self.db`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `cli_bridges/shared.py:388` (scope) | `DeliveryJournal.begin` — `self.db`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `cli_bridges/shared.py:392` (scope) | `DeliveryJournal.finish` — `self.db`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `cli_bridges/shared.py:512` (primitive-file) | `Inbox.run` — `fcntl.flock(lock, fcntl.LOCK_EX \| fcntl.LOCK_NB)`<br>Entire bridge watch/task lifetime: registry presence, Core watch transport/child and delivery-journal updates. Nonblocking single-instance guard. | C exclusion; consumer-dependent work | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `cli_bridges/shared.py:522` (primitive-file) | `Inbox.run` — `fcntl.flock(lock, fcntl.LOCK_EX \| fcntl.LOCK_NB)`<br>Entire bridge watch/task lifetime: registry presence, Core watch transport/child and delivery-journal updates. Nonblocking single-instance guard. | C exclusion; consumer-dependent work | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `config_options.py:72` (memory-declaration) | `CatalogConfigOption.__init__` — `asyncio.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `config_options.py:254` (memory-declaration) | `SessionConfigOptions.__init__` — `asyncio.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `coordinated_runtime.py:53` (memory-declaration) | `SelectedExecution` — `field(init=False, default_factory=threading.Lock)`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `coordination_database.py:95` (primitive-sql) | `CoordinationStore.begin_snapshot` — `db.execute('BEGIN')`<br>Snapshot acquisition plus caller reads/row decode, until rollback/close. | A | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `coordination_database.py:239` (primitive-sql) | `CoordinationStore._initialize_schema` — `self._connection.execute('BEGIN')`<br>Snapshot acquisition plus caller reads/row decode, until rollback/close. | A | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `coordination_database.py:252` (primitive-sql) | `CoordinationStore._initialize_schema` — `self._connection.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `coordination_session.py:51` (primitive-sql) | `CoordinationSession.transaction` — `db.execute('BEGIN EXCLUSIVE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `cursor_publication.py:52` (memory-declaration) | `CursorDelivery` — `field(default_factory=asyncio.Lock, repr=False)`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `following_inputs.py:31` (memory-declaration) | `FollowingInputs.ready` — `asyncio.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `goal_attempts.py:570` (primitive-sql) | `GoalAttemptStore.initialize` — `conn.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `goal_attempts.py:634` (primitive-sql) | `GoalAttemptStore._change` — `conn.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `goal_history.py:143` (primitive-sql) | `GoalHistoryStore._initialize` — `connection.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `goal_history.py:164` (primitive-sql) | `GoalHistoryStore._transaction` — `connection.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `input_drain.py:525` (memory-declaration) | `InputObservation._own_coordination_unchanged` — `asyncio.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `input_drain.py:559` (memory-declaration) | `InputDrain.__init__` — `asyncio.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `input_drain.py:692` (memory-declaration) | `InputDrain.drain_inbox` — `asyncio.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `maintenance_barrier.py:273` (primitive-file) | `MaintenanceBarrier.clear_stopped` — `fcntl.flock(wire_descriptor, fcntl.LOCK_EX \| fcntl.LOCK_NB)`<br>Stopped-batch/maintenance wire exclusion retained on an existing descriptor; registry/state updates and administrative worker handoff/restart work, including child-process work. No instance observed separately attributed to this helper. | C exclusion; consumer-dependent work | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `manual_compaction_bridge.py:68` (memory-declaration) | `compact_context` — `asyncio.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `optional_awareness_projection.py:102` (memory-declaration) | `OptionalAwarenessProjection` — `threading.BoundedSemaphore(1)`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `owner_restart.py:160` (primitive-file) | `StoppedOwnerBatch.accept` — `fcntl.flock(descriptor, fcntl.LOCK_EX \| fcntl.LOCK_NB)`<br>Stopped-batch/maintenance wire exclusion retained on an existing descriptor; registry/state updates and administrative worker handoff/restart work, including child-process work. No instance observed separately attributed to this helper. | C exclusion; consumer-dependent work | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `pi_native_backend.py:96` (memory-declaration) | `PersistentPiSession.__init__` — `asyncio.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `private_bus_checkpoint.py:1079` (scope) | `PrefixCertificate._install` — `db`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `private_bus_checkpoint.py:1278` (scope) | `_recover_pending_unlocked` — `db`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `private_bus_checkpoint.py:1438` (scope) | `_verify_open_checkpoint_unlocked` — `db`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `private_bus_checkpoint.py:1510` (scope) | `append_private_bus_checkpoint_unlocked` — `db`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `private_send_admission.py:58` (memory-declaration) | `PrivateSendAdmission` — `field(default_factory=threading.Lock, init=False, repr=False)`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `private_sidecar.py:136` (primitive-file) | `_locked_directory` — `fcntl.flock(lock, fcntl.LOCK_EX \| (0 if blocking else fcntl.LOCK_NB))`<br>Exclusive even for reads: read bytes, deserialize into RAM SQLite, full schema/digest/quick_check on entry AND exit, caller SQL, optional serialize/guarded replacement and fsync. No network/child in this owner; callback scope itself is generic. | B / A read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `private_sidecar.py:329` (primitive-sql) | `create_sidecar_file` — `connection.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `recovery_gateway.py:187` (memory-declaration) | `RecoveryGateway.__init__` — `asyncio.Semaphore(_MAX_CLIENTS)`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `recovery_gateway.py:209` (primitive-file) | `RecoveryGateway._lock_instance` — `fcntl.flock(fd, fcntl.LOCK_EX \| fcntl.LOCK_NB)`<br>Exclusive singleton through local recovery socket server lifetime: IPC/client handling and recovery I/O; not a global registry write lock. | C exclusion; consumer-dependent work | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `registry_store.py:56` (memory-declaration) | `RegistryCache` — `field(default_factory=threading.Lock)`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `relationships.py:693` (memory-declaration) | `ThreadRelationships.__init__` — `RLock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `restart_queue.py:284` (primitive-file) | `run` — `fcntl.flock(lock, fcntl.LOCK_EX \| fcntl.LOCK_NB)`<br>Watch/await, queue reads, worker restart work and nested queue lock for one watcher lifetime. Child-process work can occur; not a global data-write lock. | C exclusion; consumer-dependent work | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `schedule_rules.py:79` (memory-declaration) | `WakeScheduleCheck.run` — `asyncio.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `session_fence.py:20` (primitive-file) | `_try_lock` — `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)`<br>One writer per native session through one executor operation, potentially including native/provider network work. Long lifetime is intentional; no queued kernel waiters observed. | C exclusion; consumer-dependent work | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `session_fence.py:24` (primitive-file) | `_try_lock` — `fcntl.flock(fd, fcntl.LOCK_EX \| fcntl.LOCK_NB)`<br>One writer per native session through one executor operation, potentially including native/provider network work. Long lifetime is intentional; no queued kernel waiters observed. | C exclusion; consumer-dependent work | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `session_lifecycle.py:68` (memory-declaration) | `SessionLifecycle.__init__` — `asyncio.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `session_lifecycle.py:69` (memory-declaration) | `SessionLifecycle.__init__` — `asyncio.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `todos.py:399` (primitive-sql) | `TodoStore.__init__` — `db.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `todos.py:421` (primitive-sql) | `TodoStore._write` — `db.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `transcript_routes.py:160` (scope) | `TranscriptRoutes._ensure_database` — `connection`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `transcript_routes.py:161` (primitive-sql) | `TranscriptRoutes._ensure_database` — `connection.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `transcript_routes.py:193` (scope) | `TranscriptRoutes.record` — `connection`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `transcript_routes.py:259` (scope) | `TranscriptRoutes._record_publication` — `connection`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `transcript_routes.py:290` (scope) | `TranscriptRoutes.record_input_display` — `connection`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `transcript_routes.py:292` (primitive-sql) | `TranscriptRoutes.record_input_display` — `connection.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `turn_runner.py:475` (memory-declaration) | `TurnRunner.idle_backend` — `asyncio.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `turn_runner.py:494` (memory-declaration) | `TurnRunner.run_selected` — `asyncio.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `typed_table.py:810` (primitive-sql) | `TypedStore._open_writer` — `connection.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `typed_table.py:843` (primitive-sql) | `TypedStore.writing` — `connection.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `view_unread.py:244` (scope) | `NativeReplyPreparation.commit` — `database`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `view_unread.py:245` (primitive-sql) | `NativeReplyPreparation.commit` — `database.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `view_unread.py:274` (memory-declaration) | `TranscriptReadState.__init__` — `RLock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `view_unread.py:326` (primitive-sql) | `TranscriptReadState._database` — `connection.execute('BEGIN IMMEDIATE')`<br>Read/schema/row-codec or callback/DML work through commit/rollback; durable disk-store commits can fsync. private_sidecar create uses RAM SQLite under an outer file lock. No network/child assumed from a generic yield. | B preparation / C write | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `view_unread.py:361` (scope) | `TranscriptReadState.remove_threads` — `database`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `view_unread.py:385` (scope) | `TranscriptReadState._indexed_sources` — `database`<br>Potential implicit SQLite transaction context: first DML starts a transaction only when connection settings permit it; exit commits/rolls back. Caller SQL, row encoding and arbitrary body work occur here. This context alone does not prove a writer was acquired; source and runtime attribution remain separate. | C write / B caller preparation; may be read-only | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `view_unread.py:474` (memory-declaration) | `` — `RLock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |
| `worker.py:61` (memory-declaration) | `run_startup_input` — `asyncio.Lock()`<br>In-process declaration, not an observed acquisition. Cache decoding, queue/turn/native-RPC/config/identity serialization or single-builder slot depending owner; async native/turn scopes can span network/child work. Acquisition consumers are in the candidate inventory. No timing/waiter depth available from requested tools. | B decode / C serialization; consumer-dependent | not separately attributable | unknown | unknown (not zero) | source inventory; resource matches above |

## Candidate holding stacks

Outer-to-inner relationships can be read by reversing each list. Exact raw stacks, including stdlib frames and timestamps, remain in the summary/raw data. Up to five source-compatible stack signatures are shown per measured resource; fewer than five is a coverage gap, not evidence of no other work.

### S1

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.wire.lock`

- 20 observations: `agent_comms/assignment_store.py:66 recoverable_unbound_deliveries <- agent_comms/queued_input.py:448 _recover_locked`.
- 11 observations: `agent_comms/child_process.py:232 acquire_store_lock <- agent_comms/store_files.py:216 _store_lock <- agent_comms/locked_store.py:156 locked <- agent_comms/registry_store.py:149 editing <- agent_comms/registration.py:243 observe_native_phase <- agent_comms/agent_activity.py:250 observe_native_phase <- agent_comms/selected_participant.py:121 record_native_phase`.
- 7 observations: `agent_comms/child_process.py:232 acquire_store_lock <- agent_comms/store_files.py:216 _store_lock <- agent_comms/locked_store.py:156 locked <- agent_comms/locked_store.py:162 reading <- agent_comms/registration.py:365 snapshot <- agent_comms/agent_activity.py:157 _emit_activity <- agent_comms/agent_activity.py:240 _record_turn_activity <- agent_comms/agent_activity.py:251 observe_native_phase <- agent_comms/selected_participant.py:121 record_native_phase`.
- 4 observations: `agent_comms/child_process.py:232 acquire_store_lock <- agent_comms/store_files.py:216 _store_lock <- agent_comms/locked_store.py:156 locked <- agent_comms/locked_store.py:162 reading <- agent_comms/registration.py:365 snapshot <- agent_comms/goal_management.py:332 _goal_snapshot <- agent_comms/goal_management.py:329 goal_snapshot <- agent_comms/goal_management.py:323 goal_changed`.
- 3 observations: `agent_comms/store_files.py:378 _append_jsonl <- agent_comms/activity.py:380 update <- agent_comms/agent_activity.py:160 _emit_activity <- agent_comms/agent_activity.py:240 _record_turn_activity <- agent_comms/agent_activity.py:251 observe_native_phase <- agent_comms/selected_participant.py:121 record_native_phase`.

### S2

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.registry.json.lock`

- 9 observations: `/home/ts/.local/share/uv/python/cpython-3.14.2-linux-x86_64-gnu/lib/python3.14/json/decoder.py:361 raw_decode <- /home/ts/.local/share/uv/python/cpython-3.14.2-linux-x86_64-gnu/lib/python3.14/json/decoder.py:345 decode <- /home/ts/.local/share/uv/python/cpython-3.14.2-linux-x86_64-gnu/lib/python3.14/json/__init__.py:352 loads <- agent_comms/registry_store.py:107 _read_unlocked <- agent_comms/locked_store.py:163 reading <- agent_comms/registration.py:345 require`.
- 9 observations: `/home/ts/.local/share/uv/python/cpython-3.14.2-linux-x86_64-gnu/lib/python3.14/json/decoder.py:361 raw_decode <- /home/ts/.local/share/uv/python/cpython-3.14.2-linux-x86_64-gnu/lib/python3.14/json/decoder.py:345 decode <- /home/ts/.local/share/uv/python/cpython-3.14.2-linux-x86_64-gnu/lib/python3.14/json/__init__.py:352 loads <- agent_comms/registry_store.py:138 read_entry <- agent_comms/registration.py:361 entry`.
- 4 observations: `agent_comms/store_files.py:287 _atomic_write_text <- agent_comms/registry_store.py:172 _write_unlocked <- agent_comms/registry_store.py:155 save_unlocked <- agent_comms/registry_store.py:76 commit <- agent_comms/registration.py:245 observe_native_phase <- agent_comms/agent_activity.py:250 observe_native_phase <- agent_comms/selected_participant.py:121 record_native_phase`.
- 4 observations: `agent_comms/child_process.py:232 acquire_store_lock <- agent_comms/store_files.py:216 _store_lock <- agent_comms/wire_log.py:780 read_metadata_unlocked <- agent_comms/registry_store.py:209 _check_guard_unlocked <- agent_comms/registry_store.py:195 private_guard_unlocked <- agent_comms/registry_store.py:96 _read_unlocked <- agent_comms/locked_store.py:163 reading <- agent_comms/registration.py:345 require`.
- 2 observations: `agent_comms/store_files.py:292 _atomic_write_text <- agent_comms/registry_store.py:172 _write_unlocked <- agent_comms/registry_store.py:155 save_unlocked <- agent_comms/registry_store.py:76 commit <- agent_comms/registration.py:245 observe_native_phase <- agent_comms/agent_activity.py:250 observe_native_phase`.

### S3

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.bus.jsonl.lock`

- 8 observations: `agent_comms/private_bus_checkpoint.py:1510 append_private_bus_checkpoint_unlocked <- agent_comms/wire_log.py:540 _append_private_unlocked <- agent_comms/wire_log.py:386 record_context`.
- 4 observations: `agent_comms/store_files.py:292 _atomic_write_text <- agent_comms/wire_log.py:825 write_metadata_unlocked <- agent_comms/wire_log.py:510 _append_private_unlocked <- agent_comms/wire_log.py:386 record_context`.
- 3 observations: `agent_comms/store_files.py:287 _atomic_write_text <- agent_comms/wire_log.py:825 write_metadata_unlocked <- agent_comms/wire_log.py:510 _append_private_unlocked <- agent_comms/wire_log.py:386 record_context`.
- 3 observations: `agent_comms/typed_table.py:405 iterate <- agent_comms/typed_table.py:430 read <- agent_comms/cohort_schema.py:329 sealed_sequences_after <- agent_comms/cohort_foreground.py:98 accept <- agent_comms/coordinator.py:69 _run_owned`.
- 2 observations: `agent_comms/typed_table.py:406 iterate <- agent_comms/typed_table.py:430 read <- agent_comms/cohort_schema.py:329 sealed_sequences_after <- agent_comms/cohort_foreground.py:98 accept <- agent_comms/coordinator.py:69 _run_owned`.

### S4

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.native_prompt_bindings.sqlite3.snapshot-lock`

- 46 observations: `agent_comms/private_sidecar.py:246 _verify_schema <- agent_comms/private_sidecar.py:356 sidecar_connection <- agent_comms/native_prompt_binding.py:231 expected_prompt_binding <- agent_comms/native_prompt_binding.py:216 read_expected_prompt_binding <- agent_comms/historical_native_inputs.py:174 read_historical_native_inputs <- agent_comms/proven_source_coverage.py:222 native_inputs <- agent_comms/proven_source_coverage.py:214 _selected_inputs <- agent_comms/proven_source_coverage.py:154 read <- agent_comms/proven_source_coverage.py:246 prefix <- agent_comms/native_source_cursor.py:132 _advance_proven`.
- 40 observations: `agent_comms/private_sidecar.py:246 _verify_schema <- agent_comms/private_sidecar.py:361 sidecar_connection <- agent_comms/native_prompt_binding.py:231 expected_prompt_binding <- agent_comms/native_prompt_binding.py:216 read_expected_prompt_binding <- agent_comms/historical_native_inputs.py:174 read_historical_native_inputs <- agent_comms/proven_source_coverage.py:222 native_inputs <- agent_comms/proven_source_coverage.py:214 _selected_inputs <- agent_comms/proven_source_coverage.py:154 read <- agent_comms/proven_source_coverage.py:246 prefix <- agent_comms/native_source_cursor.py:132 _advance_proven`.
- 3 observations: `agent_comms/private_sidecar.py:162 _read_snapshot <- agent_comms/private_sidecar.py:350 sidecar_connection <- agent_comms/native_prompt_binding.py:231 expected_prompt_binding <- agent_comms/native_prompt_binding.py:216 read_expected_prompt_binding <- agent_comms/historical_native_inputs.py:174 read_historical_native_inputs <- agent_comms/proven_source_coverage.py:222 native_inputs <- agent_comms/proven_source_coverage.py:214 _selected_inputs <- agent_comms/proven_source_coverage.py:154 read <- agent_comms/proven_source_coverage.py:246 prefix <- agent_comms/native_source_cursor.py:132 _advance_proven`.
- 1 observations: `agent_comms/typed_table.py:542 <genexpr> <- agent_comms/typed_table.py:531 schema_objects <- agent_comms/private_sidecar.py:218 _schema <- agent_comms/private_sidecar.py:222 _digest <- agent_comms/private_sidecar.py:235 _verify_schema <- agent_comms/private_sidecar.py:356 sidecar_connection <- agent_comms/native_prompt_binding.py:231 expected_prompt_binding <- agent_comms/native_prompt_binding.py:216 read_expected_prompt_binding <- agent_comms/historical_native_inputs.py:174 read_historical_native_inputs <- agent_comms/proven_source_coverage.py:222 native_inputs`.
- 1 observations: `agent_comms/private_sidecar.py:167 _read_snapshot <- agent_comms/private_sidecar.py:350 sidecar_connection <- agent_comms/native_prompt_binding.py:231 expected_prompt_binding <- agent_comms/native_prompt_binding.py:216 read_expected_prompt_binding <- agent_comms/historical_native_inputs.py:174 read_historical_native_inputs <- agent_comms/proven_source_coverage.py:222 native_inputs <- agent_comms/proven_source_coverage.py:214 _selected_inputs <- agent_comms/proven_source_coverage.py:154 read <- agent_comms/proven_source_coverage.py:246 prefix <- agent_comms/native_source_cursor.py:132 _advance_proven`.

### S5

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.bus_meta.json.lock`

- 2 observations: `agent_comms/store_files.py:292 _atomic_write_text <- agent_comms/wire_log.py:825 write_metadata_unlocked <- agent_comms/wire_log.py:510 _append_private_unlocked <- agent_comms/wire_log.py:386 record_context`.
- 2 observations: `agent_comms/wire_log.py:784 read_metadata_unlocked <- agent_comms/wire_log.py:473 _private_marker_unlocked <- agent_comms/wire_log.py:347 _certified_source <- agent_comms/wire_log.py:304 certified_read <- agent_comms/candidate_maintenance.py:65 _drain_candidate`.
- 1 observations: `agent_comms/field_codec.py:683 decode <- agent_comms/field_codec.py:724 decode <- agent_comms/field_codec.py:298 encode <- agent_comms/wire_log.py:808 read_metadata_unlocked <- agent_comms/wire_log.py:473 _private_marker_unlocked <- agent_comms/wire_log.py:347 _certified_source <- agent_comms/wire_log.py:341 _read_certified`.
- 1 observations: `agent_comms/store_files.py:292 _atomic_write_text <- agent_comms/wire_log.py:825 write_metadata_unlocked <- agent_comms/private_bus_checkpoint.py:1509 append_private_bus_checkpoint_unlocked <- agent_comms/wire_log.py:540 _append_private_unlocked <- agent_comms/wire_log.py:386 record_context`.
- 1 observations: `agent_comms/store_files.py:287 _atomic_write_text <- agent_comms/wire_log.py:825 write_metadata_unlocked <- agent_comms/wire_log.py:510 _append_private_unlocked <- agent_comms/wire_log.py:386 record_context`.

### S6

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.input_dispositions.sqlite3.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S7

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.read_ledger.json.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S8

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.runtime_info.json.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S9

`/home/ts/.local/state/agent-comms`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S10

`/home/ts/.pi/agent/sessions/--home-ts-wt-comms-selected-tool-broker-20260926--/.2026-10-14T13-54-51-327Z_01a13ab1-c540-7168-ae58-7ac34c3a093a.jsonl.agent-comms-writer.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S11

`/var/tmp/agent-comms-live-20260927-wzjtqhza/coordination.sqlite3`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S12

`/var/tmp/agent-comms-live-20260927-wzjtqhza/bus_route_counts.v3.sqlite3`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S13

`/var/tmp/agent-comms-live-20260927-wzjtqhza/bus_route_counts.v3.sqlite3-shm`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S14

`/var/tmp/agent-comms-live-20260927-wzjtqhza/bus_page_index.sqlite3`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S15

`/var/tmp/agent-comms-live-20260927-wzjtqhza/input_dispositions.sqlite3`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S16

`/var/tmp/agent-comms-live-20260927-wzjtqhza/input_dispositions.sqlite3-shm`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S17

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.activity_latest.json.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S18

`/var/tmp/agent-comms-live-20260927-wzjtqhza/owner-restart-queue/watcher.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S19

`/home/ts/.local/state/claude-comms-inbox/watch.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S20

`/var/tmp/agent-comms-live-20260927-wzjtqhza/owner-restart-queue/.queue.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S21

`/var/tmp/agent-comms-live-20260927-wzjtqhza/private_bus_checkpoint.sqlite3`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S22

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.catalog.json.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S23

`/var/tmp/agent-comms-live-20260927-wzjtqhza/wake_candidates.sqlite3`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S24

`/var/tmp/agent-comms-live-20260927-wzjtqhza/wake_candidates.sqlite3-shm`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S25

`/home...`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S26

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.goal_waits.json.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S27

`<unresolved 8:35:3940215>`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S28

`/var/tmp/agent-comms-live-20260927-wzjtqhza/goal-private/goal_attempts.sqlite3`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S29

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.activity.jsonl.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S30

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.external_transcripts.sqlite3.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S31

`/var/tmp/agent-comms-live-20260927-wzjtqhza/external_transcripts.sqlite3`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S32

`/var/tmp/agent-comms-live-20260927-wzjtqhza/external_transcripts.sqlite3-shm`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S33

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.transcript_routes.sqlite3.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S34

`/var/tmp/agent-comms-live-20260927-wzjtqhza/transcript_routes.sqlite3`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S35

`<unresolved 8:35:4459281>`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S36

`<unresolved 8:35:3940267>`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S37

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.bus_route_counts.v3.sqlite3.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S38

`<unresolved 8:35:3940249>`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S39

`/home/ts/.pi/agent/sessions/--home-ts-wt-comms-selected-tool-broker-20260926--/.2026-10-16T16-41-22-297Z_01a14596-f07b-7588-b36d-d9c240abebaa.jsonl.agent-comms-writer.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S40

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.wake_candidates.sqlite3.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S41

`<unresolved 8:35:3940272>`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S42

`/var/tmp/agent-comms-live-20260927-wzjtqhza/.owner_release_receipts.json.lock`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S43

`<unresolved 8:35:3940264>`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S44

`<unresolved 8:35:4458243>`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

### S45

`<unresolved 8:35:3940190>`

No uniquely source-compatible holding-thread stack assigned. Timing-separated snapshots, escaped lifetimes and SQLite C locking prevent honest attribution here.

## Architecture consequences, not implementation changes

- **Wire:** phase/activity work carries the shared root barrier while it waits for registry locks; interrupted-input recovery performs SQL reads and state restoration under exclusive root exclusion. The pure goal observer also enters this barrier. Give the existing operation owner a certified snapshot/prepared change and retain only the required validation/commit exclusion; preserve maintenance, rename and no-replay guarantees.
- **Registry:** actual JSON decode occurs under the reader lock, and phase writes perform encoding/durable replacement under the writer lock. Capture owned immutable revisions before expensive read-side decoding; prepare changes outside serialization only with a correct current-revision check. Do not add another registry/cache authority.
- **Sidecar:** ordinary historical prompt-binding reads take an exclusive lock and run schema/digest/quick_check both entering and leaving the in-memory SQLite image. That is stupid because a read borrows the write-capable lifetime and validates the same image twice while everyone else waits. Put read-only snapshot validation with the existing sidecar owner and reserve serialized publication for actual changes. Preserve pending-write refusal, file-identity checks and dependent native-send currentness; this is not permission to drop the send-boundary checks.
- **Bus/marker:** immutable opened-prefix capture already exists. Extend that owner rather than creating another reader/cache; do not retain the global lock for subsequent decode or unrelated SQL projection work. Durable append/marker ordering and fsync are real constraints, not removable overhead.
- **Do not delete single-instance or session-writer fences because their spans look long.** Their purpose differs from data-write serialization, and this window shows no queued kernel waiters behind those lifetimes.
- `claude-code` owns the architecture order and assignment of implementation owners. This census does not claim a product repair or authorize installing a changed runtime.

## Reproduction and retained evidence

- Raw samples/result/owner note: `/home/ts/.cache/agent-scratch/codex-shepherd-lock-census-20261016T1630Z-final` (raw gzip is 10,016,572 bytes).
- Derived summary, source inventory, process coverage, SQLite headers and aborted-collector explanations: `/home/ts/.cache/agent-scratch/codex-shepherd-lock-census-20261016T1630Z-meta`.
- Collector/reducer/inventory/report sources: `/home/ts/wt/core-envelope-895/.test-artifacts/lock-census/`; collector hash is in `OWNER.json`. These are private measurement helpers, not product changes.
- Two earlier collectors were stopped: one had the wrong root argument; the next could not sustain 1 Hz and had an overbroad target predicate. Their partial samples are excluded. The completed run records actual cadence, failed nonblocking dumps and its affinity change rather than replacing those facts.
- Retain the completed raw recording only until the architecture report is reviewed; preserve this report, concise results and source details afterward. No user sessions, auth, wire data, UNKNOWN inputs or Codex rollout/session files were modified.
