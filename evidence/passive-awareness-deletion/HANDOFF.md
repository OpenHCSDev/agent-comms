# L0 passive-awareness closure

Owner: Cicero. Branch refactor/l0-passive-awareness-deletion-20260928.
Base: parent9e3dc3c plus published253-branch stock manual writer deletion21e3a78.
Nietzsche248 relinquished passive fixtures at69ab0c8; other suite closure remains his.

## Scope and retained user requirement

Read live #comms user messages3/6 (receiving shared traffic),63/64/65 (channel
notification checks),89 (checked versus replied) and issue84's explicit acceptance:
unmentioned B must get bounded attributed content OR precise pointers on B's next
independently admitted natural turn, without changing NoWakeDecision or crediting
UI ACK as model read. Selected-only OptionalAwarenessProjection cannot satisfy that
natural-turn requirement by itself.

Deleted passive_channel_awareness.py (399 lines), its InputDrain instance, OwnedTurn
old frame/source witness and veto, ChannelManagement rebase and ThreadManagement
caller. Deleted exclusive old tests, retained mixed relationship lock/durability
coverage under test_relationship_store.py. Existing native-only AST guard prevents
module/caller restoration. No initializer, cursor engine, alias or adapter restored.

Current path: OwnedTurn.prepare_native -> MessageBus.awareness_prompt ->
private_bus_checkpoint.addressed_source_pointers_unlocked. Existing Initials and
Addressed declarations provide the latest four source IDs/byte ranges for this
incarnation after WireMetadata.admission_after_seq. Existing FinalSeal.check_final
validates the exact checkpoint and source revisions. Optional read takes a
nonblocking bus lock and read-only SQLite connection. It does not decode source
payloads, scan full history, repair indexes, recover writes, or advance any cursor.
A missing/changed/pending checkpoint produces an explicit unavailable notice.
Pointers are untrusted context with no response/claim/read authority; older rows
may be omitted explicitly. Historical frozen membership does not imply membership
still holds. Ordinary canonical notifications, selected optional context and
archival display stay on their existing paths.

## Stores and activation

- acp_passive_channel_awareness.json and its lock: retired derived advisory state;
  no runtime reader remains. Parent can remove at quiet activation; no conversion.
- private_bus_checkpoint.sqlite3: unchanged existing runtime schema, read only in
  this feature; no reset/rebuild needed for these edits.
- bus.jsonl, metadata, registry and read ledger: durable authorities unchanged by
  awareness; no mutation, replay, UNKNOWN retirement or invented native evidence.
- Archived buses/transcripts/read state remain under the current display owners.

Compaction journal handoff now records parent19:12UTC retired_refusal correction:
97 raw UNKNOWN remain; original unchanged, send admitted, no submitted input/provider
or restart. Earlier refused fixture receipt is explicitly historical.

## Acceptance

focused.txt:14passed3.37s before pointer integration, canonical membership,
notifications, relationship behavior, archive identity/no-delivery and selected
source context. focused-current.txt records two test-fixture errors (send() returns
ID; background writer can certify a touched bus); corrected by send_message() and
altering checkpoint revision, never weakening fail-closed behavior.
pointers.txt:3passed1.14s. owned.txt:4passed1.75s including actual CommsAgent/OwnedTurn
admission, begin, prepare_prompt/open_stream/prepare_native through current canonical
NoWake receipt handling, pointer included in actual turn.task, no backend creation.
No mocked event stream, paid provider, installed/live edit, restart or message.
This proves preparation and durable reads, not a completed live native/model turn.
Ruff and diff whitespace checks pass. CI deferred; Nietzsche owns full merged suite.

Merged published parentfa293f2 into6658eefa without conflicts. Integrated actual
checks:17passed4.64s, including natural OwnedTurn preparation and unchanged archive
identity/no-delivery. Passive batch source75added486deleted; tests271added902deleted.
R0 compares this production batch0502f28a against5a4dd50. Parent separately owns
broadcast alias removal (different MessageBus methods) and has integrated manual
deletion; retain both on merge.
