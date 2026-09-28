# Selected-session cold compaction decision

Integration contract: read_selected_compaction_decision removes context_tokens,
retains context_window and exact selected provider/model/session identity fences.
Both AgentCommsCompactionSettings and CompactionSettingsData remove contextTokens.

The selected native owner determines trigger from storedContext.requiresCompaction()
or from its own getContextUsage().tokens, only when usage is known. Unknown usage
is not converted to zero. Stored admission requires compaction independently of
automatic enablement. Existing effective settings and idle/source fences remain.

NEW immutable package (canonical unmodified preparation passed):
/home/ts/wt/comms-selected-cold-compaction-native-20260928/stack/.pi-native-c6897fc58beb5b0a/node_modules/@earendil-works/pi-coding-agent

Manifest and full-tree commitment belong with this branch. Do not deploy the
package without the paired Python caller changes owned by parent. No live files,
route, launcher, session or owner were changed by this sidecar.

Real cold CLI/RPC cases and exact stale request refusals are in
 tests/test_selected_compaction_decision_native.py. Synthetic protocol exchange
coverage remains supplementary. Actual CLI tests do not issue prompts, use an
isolated selected local model and assert zero provider HTTP calls and unchanged
saved session. Parent owns the retained 137MB ACP/router/cold-send test.

The original build failed because shifted RPC patch line offsets generated an
uncommitted .orig backup, changing the committed package tree. RPC hunk positions
are corrected by the helper's three-line shift; unchanged canonical preparation
then passed all individual hashes and the whole-tree verifier. Original failed
build evidence is retained. The 194MB manifest derivation scratch was removed after validation. Main code and package preparation are otherwise unchanged.

Final validation: 18 Python protocol cases and 7 actual cold CLI/RPC cases passed.
The first native test attempt returned the correct decisions but failed its final
unchanged-source assertion because the fixture omitted saved model/thinking entries;
Pi appended those startup records. The corrected fixture includes both declarations
and proves byte-for-byte saved-session preservation. Raw failed-attempt evidence is
retained locally; the concise failure receipt is published. No provider requests
occurred. New package manifest SHA prefix c6897fc58beb5b0a.
