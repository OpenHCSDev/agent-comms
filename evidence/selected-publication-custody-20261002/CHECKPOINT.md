# Selected source/publication checkpoint

Working source for PR530, based on main87719d6f. Production: **58 lines deleted,
176 added in 13 existing files**. Not Ready: installed changed-path acceptance
has not run. No public write, owner stop, input replay or provider call.

## What changed

- `NativeEvidenceRead` owns acquisition/append/refusal cleanup for both original
  history and input readers. `NativeInputEvidenceRead` uses existing entry hooks
  to decode the original header and tracked user records. InputCommitted,
  context proof, tracked digest and post-response send verification use this
  resource. History/retained consumers retain the full reader.
- `SavedSelectedSession` keeps the already attested child when preparation did
  not change the saved source. Committed compaction still retires/reopens and
  attests through the original custody owner.
- `NativeSourcePublication`, an `UpdatedRegistration` operation, publishes the
  source on the locked current owner. Both selected and ACP attachment consumers
  use it. It neither republishes status nor manufactures heartbeat freshness.
- `TurnState` owns changed phase effects and native precedence. Registry custody
  produces those exact effects under the original lease fence. Worker, ACP
  native observations and explicit ACP transitions consume this same owner;
  TurnProgress and TurnRunner no longer reread registry after mutation.

## Owner preservation

Source publication first calls original `RegistryOwner.require_snapshot` and
`snapshot.require_active`. The replacement changes only `session_file` on that
current Thread; its active turn, turn/admission generations, process incarnation,
goal and last-finished identity remain identical. Consequently inherited
`UpdatedRegistration.installed_thread` preserves the original admitted turn.
Status and previous_status are the same canonical snapshot value; changes_owner
and needs_maintenance_admission are false. Admissions, owner generations,
last_seen and goal history are not rewritten. Actual session changes still enter
the original `publication_identity_fence` via `_commit_registration`.

Phase results are publication effects, not a nullable lifecycle result. The
lease owner refuses a stale lease by producing no effects; the current TurnState
and its phase own whether observation changes anything. Each emitted TurnState
is the exact immutable state applied to the locked document. Native precedence
continues to preserve Cancelling, Shutdown, Publishing and compaction progress.
An empty effect sequence grants no turn/input authority. Existing nullable
ActiveTurn denotes the pre-existing idle/active declaration and is not expanded.

The input projection returns no entry for non-input external records; that is
projection absence, not a turn/input disposition. Every original byte still
passes PrivateEvidenceRead JSON/duplicate-key, custody, original-prefix hash,
mutation/truncation/replacement and cleanup checks. The first physical row must
be the original header. Every message declaring inputId still uses the same
strict role/content/digest decoder and duplicate-input checks. Unrelated
assistant/tool payloads are no longer fully FieldCodec-decoded for an input
proof. Indexed context journals and SQL/input/proof identities are unchanged.
No durable/runtime schema, new cache/index or admission/replay path is introduced.

## Original372 findings and remaining failure

`original372-timeline.json` joins original claims/executions, NativeRuntimeInput
stage/inputId, native user/assistant ancestry, request diagnostics and existing
acquisition spans. All ten original replies completed; that is not full peer
closure. `original372-peer-failures.json` retains hashes of the actual two
failed peer diagnostics. Both failed before input bytes: startup slot wait zero,
spawn about20ms, get_state send about1ms, receive about18/20s without progress.
No original failed input is replayed.

The input decode/publication changes remove concrete repeated work on successful
paths. They do **not** establish why those initial get_state responses stalled
or prove that failure fixed. Native startup construction/context conversion and
parent receive scheduling remain a separate unclosed relation. No deadline,
startup slot limit or phase matcher changes.

## Source evidence / next check

`input-owner-closure.json` contains before/after declarations, annotations,
imports and consumer references using NRA audit Package/ParsedModule and Python
AST across src/agent_comms, tests and tools. Zero Python parse omissions. Native
JS/dependency source was read but not parsed or changed: no native AST closure
claim. Symbol spelling is conservative evidence, not dynamic dispatch proof.
Syntax parse and git diff whitespace checks pass; neither is installed acceptance.

Patterns: IMPL-12 shared lifetimes, BOUND-2 existing strict proof/resource owner,
IDEN-7 source publication broader than the determining fact, IDEN-3 no nullable
phase result. Next: one proportionate installed changed-path batch; parent owns
the actual configured channel acceptance and public publication.
