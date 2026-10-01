# Coordination state receiving checkpoint

Owner: Mendel. PR457. Baseline: `6feb634ba184d94d763028406152fff07027cccc`.

## Original C0/C3 source census

The original five Core C3 families are implemented at this head, rather than
being exempted as external taxonomies:

| Original site | Current owner and consumer |
| --- | --- |
| `goals.GoalMentionBinding` resolution | Declared binding members own projection; `ResolvedMentionBinding` owns its original peer incarnation. |
| Both `relationships.edit` action branches | `RelationshipEdit` command declarations own mutation and retention; both callers construct/invoke that command. |
| `acp_failure._error_detail` raw shapes | `ExternalFailureData` / `StructuredErrorValue` use the existing MRO dispatch boundary for external JSON values. No error text grants retry. |
| `maintenance_barrier.current_unlocked` lifecycle | `MaintenanceState.require_marker` joins the original marker, phase and root; the reader decodes through `FieldCodec`. |
| `thread_status.allows_control` names | Owner control declarations and `ThreadStatus.allows_owner_control` own availability. The old string capability method is absent. |

The original C1 selected-summary consumer invokes decoded data's `response`;
C2 restart state is a declared family and its existing watcher remains sole
execution owner. Parent retains the original ledger and current installed
release receipts; this census does not substitute for those receipts or close
the other owners' fresh S14 leads.

## Working state closure

- `WakeAssignment` owns initial admission and frozen acceptance equality;
  lifecycle progress is the only normalization. The nine-name immutable roster
  and eight-condition acceptance reconstruction are deleted from the store.
- `AssignmentState` constructs and validates its preengagement successor.
- `ReplayAssessments` owns its original safe-replay facts; `ExecutionRecord`
  joins these with its declared retry budget and original wire obligation.
- `DeferredExecution.can_retry` consumes terminal attempt finality.
  `TerminalAttempt` owns done/death/no-lease shape validation. The snapshot
  deletes repeated raw proof checks and the parallel retry predicate.
- `WakeAssignment.require_selected_source` compares the original committed
  source and complete frozen recipient/decision values. Both selected admission
  and framing call it; the second selected-recipient comparison is deleted.
- `AssignmentState.wake_frame` owns pending-triage versus engaged response
  behavior. Framing and `SelectedPrompt` no longer pass a string phase.
- `ClaimOwner` validates its resource, historical owner and source facts;
  `ClaimProjection` validates membership and frontier only.
- Goal source validation uses `GoalRevision`, `TextDigest`, `ThreadIncarnation`;
  the original flat wire fields remain unchanged. Missing-history incarnation
  sentinel `-1` is retained. Inputs strictly decode original text/admission
  provenance and preserve the public boundary error with its decoder cause.

No tables, persisted fields, registry format, native module, source index or
historical reader has been added or replaced. Goal/history/UNKNOWN evidence,
default runtime and all public owners are untouched.

## Verification so far

- Initial affected source selection: 36 passed, one old fake native-event test
  failed. Its exact untouched 6feb source baseline fails identically in 0.14s:
  `test_durable_turn_records_native_phases_before_completion` expects
  `tool_running` from a handcrafted `tool_execution_start` event, while the
  canonical phase remains `model_running`. Arendt owns that fixture migration;
  no production workaround or weakened assertion is introduced here.
- After source, admission and boundary changes: 38 affected checks passed in
  1.57s, including idempotency, preengagement rollback, generation fencing,
  monotonic no-replay safety, forged/unengaged wake refusal, goal mentions and
  historical incarnations.
- Resource assertion warned: home17.4GiB / RAM16.6GiB / swap9.4GiB. One bounded
  serial private native gate is proportionate; no fleet/build matrix.

## Still open before Ready

- Normal integration of Arendt's published NativeInputIdText owner, then delete
  `InputAttempt`'s competing native-ID regex. Arendt owns the native proof builder
  and redacted recovery reader consumer of the published retry contract.
- Sch owns stable-lookup scalar/source projection integration. GoalWait still
  joins original replies via today's mutable sender registry. Trace original
  certified sender/recipient refs for all wait/input-review consumers; do not
  add another query index, seen list or attribution store.
- Run the existing installed selected native local-provider journey through
  triage, full tools, one original response, source proof and lease retirement.
  Source passes above are not installed/native readiness.

Owned scratch: `/home/ts/.cache/agent-scratch/comms-coordination-state-s14-20260930`.
The disposable baseline source extraction and future wheels/stage are owned here;
original source, receipts, private proof journals and UNKNOWN inputs are protected.

## Todo receiving closure

The previous classification of the original Todo transfer witness as repo path
grammar was wrong: it was an application relation across revision, original
assignment, state, fresh generation, last operation and exact previous owner.
`TodoState` and existing `Command` operation declarations now own the complete
assign/transfer/release/state path. The store performs a transaction and applies
the original row's declared command. It contains no application decision chain.

Exact uncertain-reply transfer/release recovery checks the persisted row against
the command's complete expected result. A new operation validates the exact
current revision and previous assignment; transfer requires a fresh generation.
The original Assignment owns frozen owner/parent incarnations and generation.
No current registry name can stand in for an old creator or assignee. Done-state
declarations deny later mutations/releases and require the original assignee to
complete assigned work. No state boolean, second assignment record/store or
operation catalog has been introduced.

The original raw state/transition strings are decoded once by FieldCodec's
existing declared-family-name storage. Original six-term transfer and five-term
release retry chains, state-label branches and duplicated incarnation parsing
are deleted. All production callers are in TodoStore; the only external source
consumers are the existing Todo tests, whose one state-label assertion now uses
its declaration. The backend primitive has no existing UI/ACP command consumer.

All six existing Todo workflow controls passed in 0.73s, including the two-client
assignment race, cold reopen, exact uncertain-reply transfer/release recovery,
stale owner/generation refusal, blocking/unblocking, completion and historical
goal identity. Fresh generated CREATE TABLE SQL is **byte-identical** to 6feb:
same columns, checks, storage strings and JSON assignment/goal/previous records.
No schema reset, migration or compatibility reader is required. Original proof:
`todo-ddl-comparison.json` in owned scratch.

First installed selected-native gate at 82713f9b plus456b39 failed in 6.98s.
The retained original native journal proves all four managed tool results failed
with `OwnerGenerations.__init__` receiving positional arguments. This is Arendt's
published correction2b5918, not a framing or provider regression. Original
failed fixture/journal and source-hash/installed receipts are protected under
`native-selected-original`; no input from it will be retried.

## Installed backend checkpoint 7f64d7ac

The native-ID competing parser is deleted; Arendt's NativeInputIdText and the
corrected OwnerGenerations callers are normally integrated. Sch's reviewed
StableLookupText and corrected CoveragePage/source query checkpoint73c8ffcb
are normally integrated. This does not claim Sch460's complete UI acceptance.

The new successful-send visibility relation uses the existing PromptRequest,
live QueuedInput and durable InputAttempt owners. The original request mints
one ACP input ID, distinct from its later native input ID. The same ID is used
by initial and follow-up capture, the original durable reservation, complete
live queue projection and native-start handoff. The consumer retains the same
request and received typed native receipt on its outstanding request resource.
There is no correlation registry, text matching or independent seen/status list.

The original InitialInput is now held in the existing live queue during dispatch.
Immediate follow-ups also appear; deferred-display choice no longer controls
whether actual accepted work is visible. Clearing follow-ups cannot discard the
original dispatch. UNKNOWN alone never creates this live capability. The
original StartedInput owns admission validation and yields its native proof;
the producer retains the queue item throughout awaited InputStarted publication,
then removes it and publishes the queue. Failed/cancelled initial work loses its
live capability without being reconstructed or reclassified from disk.

Private installed source hashes match every current package Python file at
7f64d7ac. Two existing serial controlled-localhost native drivers passed:

- `native02`: installed selected triage/full read/edit/write/Bash, one original
  response and lease retirement, **11.53s**. All four actual native tool results
  are successful. Bash output proves canonical ToolRunningPhase and actual
  installed CLI help/unknown-argument entrypoint behavior.
- `queue-native01`: actual retained native history, selected summary, initial
  and queued follow-up, **11.38s**. Original request/queue/started IDs agree;
  each corresponding native user occurs once; both durable rows are Started.
  Assertions observe the original queue item still present during the awaited
  native publication. The unrelated original fixture reservation is preserved.

Original native journals, input proofs, complete source-hash/direct_url receipts
and logs are retained in owned scratch `native02` and `queue-native01`.
Three exact native ProcessIdentity witnesses from their existing diagnostics
are no longer alive. No owner/native signal, public mutation or paid call occurred.
The existing fixture teardown owns its remaining children. The failed native01
originals remain protected. Checked evidence is summarized in
`INSTALLED-CHECKPOINT.json`; this is backend acceptance, not physical UI proof.
Kepler owns Toad251 continuous send-click/queue/native-paint/UNKNOWN/cancel
acceptance and its matching consumer. Einstein458 owns coherent Core integration.

The installed TodoStore also reopened a real baseline6feb database created via
the original TodoStore and registered private participants. Exact old transfer
reply recovery leaves the original row unchanged; new generation and replacement
incarnation are rejected. Block/unblock, release/exact release recovery, cold
reopen and completion preserve the original goal. DDL remains unchanged.
This continuous installed store journey took 0.021s and spawned no worker.

Deletion accounting: Todo checkpoint84a76b18 replaces **158 deleted/340 added**
production lines in todos.py. Queue/shared-identity checkpoint7f64d7ac replaces
**27 deleted/93 added** across its seven production files. Existing bounded
ratchet over 21 claimed production files has **zero increases**, including
StringDispatch/TypeSwitch and their arms; LongBooleanChain falls13 and Todo
BooleanChainTerms falls15. No optional broad or completed gate repeat is needed.

Owned formats require no schema reset for the state/Todo/request change.
Borrowed Sch460 derived indexes retain his explicit reset/rebuild dependency;
native proof/journal/UNKNOWN carry remains Arendt's sole custody. Neither is
an activation claim from these fresh private fixtures.

## Named remaining source work

GoalWait original certified sender/recipient joins and all input-review consumers
remain assigned here. Sch has supplied existing certified_read.delivery(seq);
this will not delay publication of the useful input/Todo checkpoint.

Receiving runtime-observation follow-up from parent: RuntimeInfoStore._decode
injects missing ts=0, while AgentRuntimeInfo.timestamp default_factory fabricates
freshness if only that reader is deleted. Replace both with one required recorded
timestamp and producer-owned capture in AgentActivity.set_agent_info and
TurnRunner.prepare_selected_session. Preserve dated serialization, rename,
reopen and failed-publication semantics; reject missing dates. Runtime-only
runtime_info.json must reset at the next declared quiet Core cutover, with
durable history unchanged. Coordinate the TurnRunner method with Arendt.
This follow-up is not in 7f64d7ac and does not block the queue checkpoint.
