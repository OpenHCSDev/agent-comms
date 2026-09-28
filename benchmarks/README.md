# S7 contention measurement

Run `profile_headless.py` using an **installed** agent-comms wheel. The script
imports the installed package; it does not insert this repository's `src` into
`sys.path`.

```sh
uv venv .venv
uv pip install --python .venv/bin/python .
.venv/bin/python benchmarks/profile_headless.py \
  --scratch ~/wt/s7-disposable --output evidence.jsonl
```

The defaults execute 50, 100 and 150 registered threads, each with three spawned
poller processes calling `Comms.views.list_threads()` while the parent calls
`Comms.messaging.send()`. All registered participants have a captured live local
process identity; no agents or providers are launched. There are 20 seed messages
and 200 measured channel sends. Each case verifies the complete durable history.
The production candidate-maintenance thread still runs and is joined before root
cleanup; it is not a fourth polling process.

Each size runs twice: current shared-read policy and a benchmark-only policy that
turns shared requests into actual exclusive locks. Both call the real canonical
lock wrapper and real `fcntl.flock`; neither omits durability checks or fsync.
Process-local instrumentation restores canonical aliases and flock after use.

## Interpretation

The output records send p50/p99, poll-attempt latency, observed read failures,
per-process/per-store lock distributions, peak RSS and scratch bytes. Percentiles
are exact nearest-rank values for the recorded samples. With only 200 sends, p99
is a tail observation, not a statistically stable service-level estimate.

- `raw_flock_wait`: time inside the actual flock syscall. Includes syscall cost.
- `acquisition_including_barrier`: canonical context entry through successful
  acquisition **and** its durability barrier, including path/open overhead.
- `hold_including_barrier`: successful flock through context exit/close, including
  the barrier and small instrumentation/context-close overhead.

Lock samples cover successfully entered canonical contexts. Failed polls are
reported separately and remain failed; they are not converted into successful
reads. Publication failures abort without retrying an uncertain send. The polling
loop naturally continues after a reported fail-closed `RelationViolationError`.
Other exceptions abort. Poll distributions include both successful and failed
attempts. A nonzero failed-poll count is a product finding, not a passing result.

Shared/exclusive here is a controlled comparison of **current code**, not a claim
that A8 caused an historical speedup. Filesystem caching, concurrent host load,
current checkpointing, registration and delivery semantics can all affect the
measurements. Run order is counterbalanced by size, not randomized; no confidence
interval is claimed. See the committed S7 receipt for historical recovery details.

## Resource ownership

Use persistent scratch below `~/wt`; tmpfs and ramfs are rejected. The defaults
limit each process to 768 MiB of address space, each case to 128 MiB of scratch,
500,000 lock events and 120 seconds. These are configurable benchmark budgets,
not production limits. At least 1 GiB must remain free on the scratch filesystem.
Only three poller workers exist at a time; the parent joins or terminates its own
workers, joins production maintenance and removes its unique temporary root in
`finally`, including failures. The chosen scratch parent and JSONL receipt remain.
Do not point this benchmark at a live coordination root.
