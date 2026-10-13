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
18:01:21. This is a concrete overlapping lifetime risk, not proof of each exact
failure's cause. No more whole-bus copies under locks or live cleanup.

Existing refactor-audit Package/ParsedModule parsed all 307 current production
modules with zero omissions before implementation, enumerating declarations,
imports, checks, inheritance and consumers of response/finality/recovery. Dynamic
runtime monkeypatches and native JavaScript are outside this Python AST evidence.
Final targeted and isolated installed-process checks come after implementation.
