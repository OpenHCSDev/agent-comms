# Combined S7 bus and D2-D4 compaction ownership

Integrates complete core PR184 and185 onto main183. Source branches merged without conflicts; current callers use bus.log/bus.publisher and typed compaction states/witnesses. PairedToad PR91 changes its one production watermark consumer and removes stale C0/ThreadStatus fixture dependencies. No old APIs restored.

Combined local tests: core.txt88 passed1deselected; native.txt9 passed, including actual local native original/future queue ordering, selected-summary admission, no-goal compaction and coding-tool terminal gating. Worker complete receipts remain in s7-wire-log and compaction-closure. CI deferred.

Installed candidate runtime-bus-compaction-20260928 is built. Actual configured-provider queue/fact retention PASSED: original and accepted future input each executed once in order after compaction, all four facts retained, no UNKNOWN/errors left. Installed historical UI PASSED: original #comms/#nra rows,111 saved choices and saved transcript; live sequence unchanged. Receipts: installed-provider-queue.json, installed-history.json. Not yet activated. The first queue fixture was rejected before any provider turn because it used a /home root; the native contract requires private /var/tmp. Corrected fresh fixture retains the project/worktree under ~/wt; no production messages/history were replayed.

Parent owns matching core/Toad pins and activation after installed acceptance. Live still runtime-manual-owner-20260928 while candidate tests run.
