# PR138 relationship/passive-awareness data-boundary closure — in progress

Owner Pascal; persistent `/home/ts/wt/comms-refactor-relationships-boundary-20260928`, branch `codex/refactor-relationships-boundary-20260928`. Started from main e98ffb5/PR164; parent now integrating PR165. No live writes, activation, provider calls, helpers or model changes.

Implemented typed RelationshipDocument/PassiveAwarenessDocument stores and actual typed consumers; deleted raw mapping store APIs, duplicate-edge read projection, from_payload filtering, duplicate decoding and path forwarding aliases. Collaboration records own identity/orientation/update behavior; document owns edit/order state. Current shape is strict, no preservation of hypothetical extension fields. Real rename aliases and missing/rebound historical endpoints remain.

Relationship v1 to v2 is an explicit deployment migration in relationship_migration.py, using A8 atomic publication. Normal reads never call it or accept v1. It merges duplicate pairs once, preserving original records/notes/timestamps/orientations as history. Passive shape stays v1, decoded directly to a typed document; invalid optional files stay untouched.

Read-only inspection: active route `/var/tmp/agent-comms-live-20260927-wzjtqhza` has neither file. Original `/home/ts/.agent-comms` holds17 relationship records,2 sort rows and85 passive rows; no unknown fields or duplicate relationship pairs. Preview converts all17+2 and retains one renamed original as history. No actual migration applied there.

Current acceptance work: baseline78pass2preexistingfixturefail; first current77pass1removed-path-fixturefail, migration40pass1fixture-namefail. Both new fixture issues fixed, pending coherent rerun. Also updated old registry failure injection to the actual RegistryStore writer and a stale lock-observer fixture signature.

Exact NRA invocation: `sh evidence/relationship-boundary/nra-command.sh`, from this worktree. It pins both `--scan-budget-seconds 140` and `timeout 165`, disables cache, bounds workers to1, and supplies explicit full package context. Baseline79/79 detectors completed, no omissions/findings. Final scan and tests pending. Parent retains serial deployment and source-root migrations; no CI wait.

## Completed local behavior before current-main rebase

`core2.txt`:88 passed. Includes real concurrent child readers/writers, current ACP InputDrain/passive frame tests, live registry rename/delete/rebind semantics, explicit duplicate migration/idempotence, actual migration CLI preview/apply separation and injected file/replace/directory-sync rollback.

`consumers.txt`:54 passed (tool entry points, envelope integration and shared LockedStore). `copied-data-acceptance.json`: all17original relationships/2sorts survive migration of an owned copy,85passive rows/56witnesses roundtrip exactly. The proof script is retained; user notes/registry copies are disposable local test data and are not published.

NRA final scan completed79detectors/0omitted/0reported findings in21s. Exact successful commands preserved in `nra-command.sh` and `NRA-S3-COMMAND.md`. Commands use full package context,1worker,`--no-cache`, explicit140s internal/165s shell budgets. Direct source edits/scripts are authored ownership decisions; no native codemod equivalence claim.

No known implementation blocker. Before parent switches the original root to this code, preview and apply relationship v1 migration with `PYTHONPATH=src <integration-venv>/bin/python -m agent_comms.relationship_migration --root <root>` (preview), then the same command plus `--apply` under parent deployment ownership. Active route has neither store, so fresh active-root writes create the current schema. Passive data needs no format migration. No automatic old-schema reader is retained. Unknown or conflicting actual data is rejected before publication rather than discarded. No live files were changed here.
