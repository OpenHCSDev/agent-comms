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
The exact historical owner disposition below is needed before the coherent
shared implementation and normal recovery caller can land; this is not a
generic code-approval request. No shell
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
No resolver acceptance tests ran because production implementation was prevented;
the requested concurrent fresh-event and installed settlement acceptance remains
outstanding. Independent #773 thinking-override work is pushed separately, not
counted as resolver progress.

## Exact retained file claims after the reported seq 4053 release

A read-only observation through sealed bus seq 4114 used the existing
`CertifiedSourceRead.messages` and `project_verified_transitions` owners.
Context rows were skipped by their checkpoint geometry; no whole-bus copy or
physical lock was taken, and both SQLite connections were read-only and short.
All four claims below still existed in that later cut:

The later complete inventory, including these four claims, is recorded once in
[terminal-claim-inventory.json](terminal-claim-inventory.json). It carries the
exact owner/incarnation, execution/attempt/scope, path/generation, creating
message, taken UTC time and terminal time, grouped by original attempt.

Existing `AttemptRecord` decoding confirms revision 55, attempt_failed,
native_terminal_failure, backend_done/process_dead true and tool_admission_open
false. Original `NativeRuntimeInput` 4464a2800c5279ec9fbb0e5b6d98f2ea remains FULL.
Current registry observation: admission 12441, turn
61b09a660f4e3d0ef8c6ff78cd48e9be, PID 2218857. The old admission cannot pass
`CodingToolOwner.admit`'s exact current-turn checks (`channel_coding_tools.py:103-119`,
`claim_admission.py:74-85,110-111`, `attempt_states.py:60-62`); no live coding
owner can validly admit it. This does not introspect retained Python objects.

The coach needs to arrange disposition of these exact terminal-attempt claim
generations through the existing release owner. No foreign release, direct SQL
mutation, pointer clear, restart, replay or denied-edit shell bypass occurred.
The diagnosis remains documentation-only until implementation can land; move
that diagnosis to the PR comment then, not count it as a production repair.

## Failure inventory and unified ending request (4101 / 4149)

Read-only cut 4163 found 57 retained claims: **18 belong to terminal attempts**
(14 core-goal-retry, 4 core-route-counts), 39 to nonterminal attempts, zero unbound
or missing/mismatched attempt identities. The JSON inventory is a dated source
observation, not a current release permit. All 39 nonterminal claims are excluded,
including three core-batch-reply settling claims and two slop-census compaction
claims. Closed tool admission alone is not permission to release an active attempt.

The resolver claim was taken 18:09:19.394484 UTC; its attempt ended
18:16:49.303 UTC with native_terminal_failure. The route-counts attempt ended
18:01:19.210 UTC. Both precede the 19:08 stop: retained earlier failed claims,
not claims stranded by that later stop. No claims or stores were changed.

Source relationships at this checkpoint:

- `DurableTurn.fail_terminal`, durable_turn.py:132-147, calls
  `AttemptStore.settle_nonpublication`, attempt_store.py:306-322, then
  `RecoverySnapshot.settle`, coordination_snapshot.py:214-281.
- UNKNOWN and interrupted owner recovery also call that existing snapshot
  settlement. Current source already calls release_resources on that route
  (coordination_snapshot.py:231). Retained history alone does not prove that the
  current deployed producer omitted it; the old producer's runtime pin is not
  established by the retained launch log. Do not misattribute this to the stop.
- Publication completion has a separate terminal writer at
  coordination_snapshot.py:283-320 and a second release call at line 292.
- Existing AST tooling parsed 307 production modules, zero omissions. It found
  one release_resources declaration and two consumers; two terminal AttemptRecord
  update sites, both in RecoverySnapshot. Other AttemptRecord writes are live
  observation/renewal; goal_attempts.py is a different table owner. Dynamic phase
  arguments are not statically resolved. Manual releases use Publisher's same
  envelope primitive and are not competing terminal-settlement implementations.

The proposed coherent change makes RecoverySnapshot._finish_attempt own exact
finality, resource release, terminal attempt/execution/response/assignment writes
and current-pointer retirement for both existing entrypoints, deleting both
copied terminal write blocks and both outcome-local release calls. It preserves
published response states without rewriting their immutable data. The edit was
refused in full on **coordination_snapshot.py**, generation
b72e6ecc6c1245a3872844022d8f638b, held by the same earlier terminal attempt. It
landed no code. The release implementation and ordered native boundary also
remain claimed. A different file/helper/store would bypass rather than fix this
owner, so none was added. There is no after-search or behavioral acceptance to
claim yet; the exact old owner disposition is the implementation blocker.
