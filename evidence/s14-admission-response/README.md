# S14 admission / response identity closure

**205 production lines deleted; 284 added.** Source `e7c5a765` includes current
main `d4f09edf`. Boyle owns PR350; parent owns integration, installation and the
live-root acceptance. PR351 owns the entire optional-awareness rewrite.

## Deleted and migrated

- Delete selected admission's 22-condition cross-record identity/state chain and
  its repeated registry nullable checks. Actual WakeAssignment/WakeAdmission
  share AssignmentBinding; participant and attempt use existing OwnerGenerations;
  RecoverySnapshot requires its validated current attempt. Existing assignment
  and attempt lifecycle declarations own engagement and tool/publication legality.
- Delete LiveResponseOwner's seven-field scalar mirror and its separate process/
  registry predicate chain. Capture actual Thread; reuse RegistryOwner with actual
  ThreadIncarnation, ProcessIdentity (including process birth), active turn and
  admission. The response subtype preserves goal-independent settlement; ordinary
  native input/awareness still uses the goal-sensitive check subtype.
- Delete repeated attempt token/owner comparisons in AttemptStore and terminal
  response. Actual OwnerFence and AttemptRecord project one AttemptAuthority.
  Revision conflicts remain separate; a value identity is not permission or proof.
- Delete seven-field `_intent_matches_request`; compare the canonical expected
  Message envelope. MessageReference is the common sequence/content-ID identity
  used by Message, WakeAssignment, WakeAdmission and PublicationReceipt. Its
  lower dependency module avoids the Message/claim cycle. No schema/wire fields
  change, no second codec, adapter, registry or compatibility constructor.
- Migrate the real SelectedExecution response witness and affected tests. Keep
  source/cohort/receipt, exact target, current generation, active process/turn,
  dispatch uncertainty and terminal receipt fences. Lock order and registry/bus/
  SQL custody through durable selected-file write remain unchanged. No auto-resend.

## Ownership method and guards

Reread global AGENTS, NRA and authoritative refactor-audit archive/current resolved
skill, catalog README and relevant identity/state/boundary patterns. Apply IDEN-1,
IDEN-3, IDEN-8, IMPL-10, BOUND-1/2 and TIME-9. Reuse ReservationRule only for
existing registry checks, rather than inventing one rule per identity term.
A new assignment lifecycle owns engagement on its declaration; a new process
identity component belongs to ProcessIdentity, with no second response tuple.

[Bounded changed-path measures](changed-measures.json), via installed shared
Measure family against `d4f09edf`: chain terms **-95**, long chains **-11**,
foreign absence probes **-10**, type identity checks **-3**, god-class excess
**-13**; codec subclasses and raw string subscripts unchanged. **No changed-file
increase**. No new class exceeds500. This is a bounded source measurement, not a
global NRA analysis or whole-S14 completion claim. Existing retry/publication
state chains outside this identity slice remain on the original S14 queue.

## Actual affected acceptance

Install noneditable `.[acp]` into this WT's .venv; imports are recorded in
[installed-paths.json](installed-paths.json). Tests run serially with `-o addopts=''`
and a bounded owned `.scratch` root. No paid provider, live data mutation or CI wait.

- [Installed lifecycle](integrated-lifecycle.log): **42 passed in7.54s**.
  Actual SQLite/cohort/fence/claim/response stores, concurrent writer exclusion,
  ambiguous append/no retry, exact terminal receipt, rejected process birth reuse,
  S3 legality family and retry/generation behavior. Command modules:
  `test_coordination_response`, `test_claim_admission_verifier`,
  `test_native_admission_rules`, `test_s3_legality_behavior`, and
  `test_coordination_store::test_retry_partition_and_generation_fence`.
- [Actual native journey](integrated-native.log): **1 passed in3.44s**.
  `test_selected_execution_native::test_native_full_four_tools_publish_and_release[False-True]`
  uses the actual pinned Pi package, selected execution/claim/write, native input
  journal, response publication and claim release; only the provider is local and
  controlled. Package: `/home/ts/.local/share/agent-comms/native-current-d3967e8b6ee0cf28/node_modules/@earendil-works/pi-coding-agent`.
- Ruff F/E9/I and `git diff --check` pass. Earlier RED fixture-construction evidence
  is preserved in [response-first.log](response-first.log); the replacement-PID
  fixture now keeps its captured Thread internally consistent and exercises the
  actual refusing boundary. PID reuse is explicitly in the parameter family.

## Coordination and limits

I missed the PR351 awareness ownership notification. The overlapping uncommitted
awareness implementation and awareness-only TypedTable.require were withdrawn;
PR350 changes neither file. See [overlap-resolution.md](overlap-resolution.md).
Earlier awareness/performance and intermediate tests describe that withdrawn
local draft and are historical only. Final readiness rests on the integrated
logs above. PR351 has its own canonical batched-read acceptance and timing.

No blocker in this source slice. Parent can integrate350 with351 and verify the
affected live runtime. This receipt does not claim installation, live-user
acceptance, all S14 chains, T4 closure or the final performance target complete.
Resource warning prevented new agents/global scans; owned disposable artifacts
are cleaned after recording these results.
