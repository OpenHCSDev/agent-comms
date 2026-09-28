Fix recovery after a failed selected compaction without replaying user input.

A turn that ends before durable native input binding now retains a NotSentInput notice rather than an UNKNOWN that blocks every later compaction. Bound uncertainty remains UNKNOWN and cannot be retried automatically. Selected correlated provider failures have a terminal FailedSummary state with no input admission. The paired native patch supplies its attested failure receipt.

An interrupted automatic summary can be retired only under the native writer, owner, registry and input fences, with exact unchanged saved-source revision, matching owner and original text, no input binding and no associated native commit intent. Provider outcome stays unknown; original input/history remain unchanged. No send token is minted. A fresh user input requests its own summary.

13 focused tests passed, including actual native preparation plus SQLite recovery/refusal. Owner-authorized real-provider installed ACP fork of actual137MB history passed153.6s, committed summary and returned LIVE_COMPACTION_OK without touching original history. That receipt proves the clean full path; installed failed-attempt recovery and paired native failure handling are still being completed. CI deferred. No live activation of this new recovery patch yet.
