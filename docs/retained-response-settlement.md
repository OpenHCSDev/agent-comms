# Retained response settlement

Owner: core-goal-retry. Branch: fix/resolve-retained-responses.
Separate from held draft #780. No goal-family edits.

Read-only retained SQL confirms execution
wirev13d8de4adfbb8e011818c5e6c1c33b8fe8f4fc1512bdd6ea94f6e3b25e3d253b8,
ordinal 1, backend_done/process_dead true, current pointer revision 43, frozen
claude-code reply and append dispatch, but no publication receipt. Original
input and provider operation are not replayed; no owner restart or pointer clear.

At bf5b1ab49, coordination_response.py:600 has resolve_existing_response but
only test consumers. Its live fence/owner requirement cannot represent a retired
turn. selected_participant.py:182 refuses an occupied pointer before admitting
new work. The frozen append dispatch and exact durable keyed response are the
existing evidence for SQL-only completion; absence must never append.

Move shared recording/completion into PublicationAppendDispatches and invoke
read-only resolution through the existing recovery owner before a fresh selected
turn. Preserve live-owner checks on first publication. Resolve all frozen routes;
partial/absent evidence remains blocked. Validate original attempt, native root,
source cohort, intent, dispatch and exact durable response under the existing
ordered boundary. Do not manufacture a secret live fence from stored digests.

Transaction incident: f7b7d724's one-time cleanup held CoordinationSession.read
across backup copying and release appends. In rollback-journal mode that read can
block another writer's COMMIT; the pending writer can then block BEGIN IMMEDIATE.
Backup bus mtime 18:00:57 UTC, SQL 18:01:15, result 18:01:19; reported drain failures
18:01:21. Coach confirmed this caused five agents' database-lock failures. No more
whole-bus copies under locks or live cleanup. The large completed backup is
retired at coach request; the small backup files remain. Its append-only live
prefix observation: 4,280,444,519 bytes, SHA-256
4cb35a98ed0614f3e0977d75acf50332d5faf0f3d2401efe90bb8d2dacaae0ef.
The copy was hashed without live locks before retirement. Released count: 54;
15 other claims preserved. Writing a scratch manifest was refused as outside the
project; this existing evidence document retains the observation instead.

Existing refactor-audit Package/ParsedModule parsed all 307 current production
modules with zero omissions before implementation, enumerating declarations,
imports, checks, inheritance and consumers of response/finality/recovery. Dynamic
runtime monkeypatches and native JavaScript are outside this Python AST evidence.
Final targeted and isolated installed-process checks come after implementation.

## Actual checkpoint / blocker

The complete producer/resolver edit to src/agent_comms/coordination_response.py
was refused: Resource already claimed by 'core-goal-retry'. It made no changes.
The owner change is not implemented and no alternate resolver or caller was added.
Review needs to authorize settlement of that exact retained file claim before the
coherent shared implementation and normal recovery caller can land. No shell
bypass, provider replay, restart or direct pointer/database mutation was attempted.

Read-only checkpoint pointers and the existing DeliverySources decoder independently
confirm exactly one durable reply: seq 3782, id 19d2072582f5, core-batch-reply to
claude-code, with the frozen publication key for the retained execution. Only that
indexed byte range was read; no whole-bus scan/copy or physical locks were needed.
The SQL publication record is still missing. The exact attempt remains unresolved.

The proposed change makes PublicationAppendDispatches own common recording and
completion, used by both the live publisher and existing-row resolver; it deletes
the allow_append dual-mode function. The resolver consumes the recorded immutable
attempt, not a fabricated secret live fence. RecoveryMonitorCapability then resolves
the original routes before SelectedParticipant checks whether it can admit fresh
work. Absent/mismatched durable replies continue to refuse without append or input.
No new tests run because production implementation was prevented; the requested
concurrent fresh-event and installed settlement acceptance is still outstanding.
