# Native cold-compaction closure

Implementation checkpoint c9777c26 and native/caller fixture followthrough96341435
are pushed in267. Final followup corrects the settings-boundary assertion and
publishes test receipts; production native package remains905f9f6facb3070a.

Mandatory selected window + PiCompactionSettings flow through helper, prepare,
bridge and runtime. Parent owns the two adaptive/manual callers and the retained
ACP test; every other current direct Python/JavaScript caller in this scope is
migrated. No default window, default reserve, keep_recent_tokens shim or
allow_split_turn refusal remains in those preparation APIs.

Actual native compact + localhost SSE: whole-turn1request/66320bytes and
split2requests/104876bytes, complete500-path ledger and15KB generated responses;
both save/reopen into ReadyContext under191712byte policy. No paid request.
Native parallel hierarchy/current caller checks passed.
22 boundary/runtime/protocol checks passed2.89s.7 other direct-owner preparation
checks passed in the earlier focused shard; its final old constructor-exception
assertion failed and was corrected/tested in the22-case completion receipt.

Parent independently reports both actual137MB cold manual/adaptive sends passed,
then installed-wheel acceptance passed using143686055byte real-history copy,
10localhost calls,601read+211modified paths retained, one original input, idle
and40.77s. Parent owns those actual receipts and deployment.

Limits: broad caller shard reached60s after36passes, not claimed green. Earlier
synthetic compaction harness failed an old concurrent progress-phase equality
assertion; selected synthetic400000-character history exceeded its3s observation
timeout. Neither is claimed green or used to prove installed readiness. No
optional test expansion performed after parent's stop instruction. CI deferred.

Parent may merge267with266. No sidecar live deployment or edits to parent files.
