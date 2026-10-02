# Isolated pre-A8 recovery

Recovered production revision `9cb6651fa07d3613587ecf10db53d038c3d63a98`, the parent
of `0b7f82e1` (first LockedStore/A8 adoption). Its installed `_store_lock` uses
LOCK_EX for every acquisition. This is a recovered source baseline, not a
reconstruction of the post-mortem's original machine state or dataset.

Own detached worktree: `/home/ts/wt/comms-s7-baseline-20260928`.
Installed its unmodified source with `uv pip install --python .venv/bin/python
'.[acp]'`. No changes to historical production files. The measurement driver alone
was translated to that revision's owning public API, including its explicit
private/claim protocol setup and PID registration contract. Its original canonical
lock and durability barrier execute; there is no synthetic shared baseline.

The historical driver is an ephemeral recovery probe, not a compatibility path in
the current benchmark. The exact translation from the benchmark at `dca49c8d` is
retained as `historical-driver.patch`. To reproduce in the detached checkout:

```sh
# Copy benchmarks/profile_headless.py at dca49c8d to historical_profile.py.
# Copy benchmarks/lock_observation.py at dca49c8d to lock_observation.py.
patch -p1 < historical-driver.patch
uv venv .venv
uv pip install --python .venv/bin/python '.[acp]'
.venv/bin/python historical_profile.py \
  --scratch /home/ts/wt/comms-s7-baseline-20260928/.scratch \
  --output historical.jsonl --threads 50 100 150 --sends 200 --seed 20 --seconds 180
```

Identical registered counts, send/seed counts, three-poller topology, scheduling
intervals, instrumentation, limits and durable-message checks. It ran after the
current matrix, never concurrently with additional benchmark pollers. Both versions
use their own real private publisher and claim durability barrier. Current setup
also installs its now-canonical checkpoint/runtime schemas; the earlier setup does
what its historical production entrypoint did. Numerous refactors, current codec
and checkpoint behavior differ between these revisions. Thus an observed latency
difference cannot be attributed to step 1/A8 alone. The current-code shared versus
forced-exclusive comparison isolates the read-lock policy more directly.
