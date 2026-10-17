# P118: durable locations and typed host boundary (draft)

Integration owner: core-native-runtime. Main base `05b0162bc`; frozen
`work/core-pi-durable` (`b1fa2b97f`, #1127) was merged normally without editing
that branch. PiNativeBackend remains the new-thread default.

## Location ownership

DurableDatabase belongs to P117's RuntimeLocation family. The path remains the
persisted session_file; no second location envelope exists. AgentBackend's
subclass declaration binds location_type._backend once; RuntimeLocation reads
that declaration and UnstartedRuntime returns its existing owner. TurnRunner
selects through Thread.runtime.backend.

DurableDatabase supplies history, read keys, selected-source identity and storage
files. It refuses unsupported durable forks and native Pi attestation instead
of treating SQLite as a native journal. DurableDatabaseFiles reuses the existing
file owner's process-reference checks and deletion lifecycle. Schema-report
consumes file-owner schema_paths rather than assuming native proof files.

A paused DurableHost reads the existing root without taking the worker's
exclusive lock, resuming work, loading provider credentials, or creating a
missing database. SDK 1.3.0 still uses a read/write storage opener and idempotent
schema initialization; this is not a read-only SQLite capability claim.
History round-trips the SDK's opaque JSON continuation, page.next. The frozen
host incorrectly read page.cursor and stopped after one page.

## One inbound decoder

DurableHost.read parses each JSONL line and calls FieldCodec.decode(HostMessage,
...). HostMessage members deliver typed events, typed results, or typed errors.
No caller selects a decoder or reads a raw host-result/event dictionary.

- HostCommand declarations own request fields and the expected reply type.
  PendingHostRequest owns the future. A mismatched reply fails while the pending
  request is still owned, so the reader can finish its future with that failure.
- DurableEvent and DurableChange use the SDK's type discriminator. Text and
  thinking delta members create Chunk and Thinking themselves; tool-start and
  task-error records create their Core events. No payload bag or string chain
  remains. Event vocabulary derives from DurableEvent.names, not a forwarding
  wire_kind method.
- CompactionHostMessage decodes directly into the existing CompactionOutcome
  family: NothingToCompaction, PlacedCompaction, StaleCompaction, FailedCompaction.
  There is no second compaction-status family or consumer-side reconstruction.
- Configuration, context, recovery inspection, settlement and history are typed
  replies. SDK usage counters reuse PiUsage. SDK-defined arbitrary JSON data,
  including tool arguments, opaque cursors and uninterpreted stored message or
  extension data, remains JSON-valued fields; consumers do not interpret these
  as host protocol shapes.
- Node and Python use the new frame/request format together. The previous flat
  dict reader and caller-side history decoder are deleted; no alternate codec,
  compatibility reader or FieldCodec subclass exists.

TranscriptSource owns saved-record projection and input-bounded turn reading;
records own their boundaries and TurnDigest assembles events. The replaced
native loop and external projection copy were removed. External SQLite readers
retain their efficient turn query.

## Verification

- Original targeted batch: **39 passed**, with the same two known main failures
  excluded explicitly. Earlier full runs and an unmodified-main reproduction
  retain both failures: malformed_unknown_and_partial_entries_keep_byte_boundaries
  and native_ancestry_is_acquired_before_coordinator_read. Neither is green.
- Additional boundary batch: **10 passed**. Covers all four decoded compaction
  outcomes, nested event/change records, strict malformed-record rejection,
  reply/command association, and a real-host error followed by successful typed
  context/inspection replies. Combined: **49 passed, 2 deselected**.
- Real SDK/SQLite history contains 260 newly created entries. All pages read in
  both directions while a writer host remains open; newest-turn reading works
  and interrupted-work inspection remains unchanged. No model input submitted.
- Requested single-quoted string-subscript grep: **0**. Backend equality-dispatch
  grep: **0**. Python compaction status comparisons: **0**.
- Unchanged debt ratchet against 05b0162bc: **exit 1, SourceLines +908 only**.
  BannedVocabulary in external_transcript decreases by one; every other delta
  is zero and introduced findings are empty. In particular StringSubscript,
  StringDispatch/arms and FamilyFlattened no longer grow. No budget, threshold
  or scope changes.
- NRA parser, syntax index and canonical family index cover Core src/tests/tools
  and Toad src. Before accepted-head refactor: 1,064 modules, 4,523 original
  classes, 4,262 canonical. After: 1,065 modules, 4,579 original classes, 4,318
  canonical. Zero parse omissions; the same 261 local/conditional/ambiguous
  declarations remain OPEN. All 97 backend declarations have a canonical
  projection. Cross-root attribute/name candidates do not prove dynamic binding.
  JS and pinned SDK declarations were read semantically, not Python-AST parsed.

Evidence: owned checkout `.artifacts/p118/`, including codec-checks-b.txt,
codec-ratchet-final.json, codec-{before,after}-family.json and retained main
failure reproduction. Python source shortstat versus main: **11 files +956/−48**,
net **+908**. Existing Python and borrowed frozen npm dependencies were reused.
The ignored node_modules symlink is absent from the final Git tree and PR diff;
one earlier published checkpoint accidentally included it, then removed it.

## Still outside this checkpoint

Provider submission stays disabled until initial Core InputId and send-disposition
integration is connected. Event conversion and structured tool/thinking history
are incomplete; durable forks, native-selected execution, recovery crash/reopen
and configured-provider worker/UI acceptance are not claimed.

No original saved session or UNKNOWN input was replayed. No installed runtime,
Toad source or provider configuration changed. The saved-copy/provider journey
is a separate assignment.

Dependencies remain pi-durable, pi-ai and chord 1.3.0, installed with
`npm ci --ignore-scripts --omit=dev` in src/agent_comms. The host retains the frozen
internal estimateContext import because 1.3.0 does not publicly export it;
place-queued-now also remains unavailable. Neither upstream gap is closed.
