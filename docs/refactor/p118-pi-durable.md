# P118: durable runtime location integration (draft)

Integration owner: core-native-runtime. Base main `05b0162bc`; frozen
`work/core-pi-durable` (`b1fa2b97f`, #1127) was merged normally, without editing
that branch. PiNativeBackend remains the new-thread default.

## Owners

- DurableDatabase is a member of P117's existing RuntimeLocation family. Its
  path remains the persisted session_file; no second location envelope exists.
- AgentBackend's subclass declaration binds location_type._backend once.
  RuntimeLocation.backend reads that declaration; UnstartedRuntime returns its
  existing owner. TurnRunner selects through Thread.runtime.backend.
- DurableDatabase supplies saved history, read keys, selected-source identity
  and storage files. Native Pi attestation and unsupported durable forking fail
  explicitly, rather than interpreting SQLite as a native journal.
- DurableDatabaseFiles reuses NativeSessionFiles' process-reference checks and
  deletion lifecycle, overriding the actual SQLite/lock members. Schema-report
  consumes each file owner's schema_paths instead of assuming native proofs.
- DurableHost owns the Node protocol and child. A paused history host reads the
  existing root without taking the worker's exclusive lock, resuming work,
  loading provider credentials, or creating a missing database. SDK 1.3.0 only
  offers a read/write storage opener: history still runs its idempotent schema
  initialization. This is not a read-only SQLite capability claim.
- DurableHistoryPage decodes original host pages once through FieldCodec.
  Its opaque SDK cursor is JSON, not a string. The SDK continuation is page.next;
  the frozen host incorrectly read page.cursor and stopped after one page.
- TranscriptSource owns saved-record projection and input-bounded turn reading.
  NativeRecord and durable input records own their boundaries; TurnDigest owns
  event assembly. The replaced native turn loop and external projection copy
  were deleted. External SQLite readers retain their efficient turn query.

## Verification

Real Node/SQLite host, no provider requests and no comms tool inputs:

- Durable/location checks: four pass, including persisted/unstarted round trips,
  worker selection, missing history, source identity and file members.
- Existing durable event checks: two pass.
- Expanded existing backend, external transcript, schema and native transcript
  batch: 39 pass, two fail. Both failures reproduce on unmodified main:
  malformed_unknown_and_partial_entries_keep_byte_boundaries and
  native_ancestry_is_acquired_before_coordinator_read. They are not green.
- The real SDK writes 260 fresh entries; history traverses all pages in both
  directions while a writer host holds the database, and reads the newest turn.
  Original interrupted-work inspection stays unchanged.
- Required backend equality-dispatch grep: zero.
- Unchanged full debt ratchet: exit 1. Python source grows by 513 lines; retained
  frozen-backend protocol code also contributes FamilyFlattened +1,
  StringDispatch +1, StringDispatchArms +3 and StringSubscript +32. No newly
  introduced banned vocabulary. No checker scope/budget/threshold changes.
- NRA Python census: before 1,011 parsed modules plus the unresolved merge
  conflict in agent_backend.py; after 1,013 modules, zero parse omissions.
  Includes Core src/tests and Toad src consumers. Dynamic resolution stays open;
  JS and pinned SDK declarations were read semantically, not Python-AST parsed.

Evidence in the owned checkout: `.artifacts/p118/`, including source census,
full check output, main failure reproduction and ratchet JSON. Tests use existing
installed Python dependencies and borrow the frozen checkout's npm dependencies;
node_modules is absent from the final Git tree and PR diff. One earlier published
checkpoint accidentally included its symlink; the following commit removed it.

## Not delivered by this checkpoint

Provider submission remains deliberately disabled until initial Core InputId and
send-disposition integration is connected. Event conversion is incomplete; the
current saved-history projection retains the frozen host's text projection,
not full structured tool/thinking history. Durable forks and native-selected
execution are not claimed. Recovery crash/reopen acceptance is unfinished.

No original saved session or UNKNOWN input was replayed. No default runtime,
installation, Toad source or provider configuration changed. The configured
provider saved-copy/worker/UI journey is a separate assignment.

Pinned dependencies remain pi-durable, pi-ai and chord 1.3.0. Install with
`npm ci --ignore-scripts --omit=dev` in src/agent_comms. The host retains the
frozen internal estimateContext import because 1.3.0 does not publicly export
it; place-queued-now also remains unavailable. Neither upstream gap is closed.
