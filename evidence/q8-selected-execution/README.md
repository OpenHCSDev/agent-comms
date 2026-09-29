# Q8 whole selected execution closure — PR384

## Deletion first

Removes the 690-line partially initialized SelectedExecution role, three ACP write callbacks, its repeated instruction/tool/post-write dispatch and raw triage JSON key inspection. The runner now orders declared lifetimes; it no longer carries optional progress/native-input/lease scratch, dynamic assignment/model/token/prompt fields or callback completeness checks. All production/result/file-write imports and test callers use their actual owners; no compatibility reexports. No new store, native package, codec or send authority.

- SelectedParticipant owns one exact registry lease and its accepted frozen source. Uses existing RegistryOwner/ParticipantOwner and complete FrozenRecipient/WakeDecision values. Lexical cleanup covers source/preparation failure too.
- SelectedSession consumes first-start authority when triage returns. Fresh enrollment uses existing RegistryOwner.capture/require_exact under original wire/bus/registry/store/journal ordering, preserving whole Thread equality, active state and admission. Creation coverage remains available on the final result.
- SelectedAttempt can only exist with FullNativeSend/DurableTurn. SelectedRequest can only exist with a reserved PrivateSendAdmission. Stage declarations own UNKNOWN settlement; no optional phase probes or input reconstruction.
- SelectedAction declarations own instructions, native tool binding and post-model effects, reusing CodingToolOwner and actual claimed-write publishers. ACP's bound controller authority retains the existing operation/controller map and durable SelectedWritePlans; no second store. Planned writes retain one plan rather than duplicating its resource/contents.
- SelectedTriage uses canonical FieldCodec and declaration-derived membership; IGNORE owns its two atomic disposition edges inside the existing proof transaction, FULL retains deferred-to-engaged semantics. Strict JSON/duplicate-key/bounded decoding remains.
- Small existing records own their required behavior: NativeTurnResult.require_publishable, CurrentExecutions.require_idle, AssignmentState.requires_selected_triage. No native proof, InputDrain, MessageBus/history or attempt-store implementation changes.

Patterns: IDEN-1/3, IMPL-4/5/8/10/12, BOUND-1, TIME-9. Latest NRA/refactor-audit skill and relevant catalogs reread. No per-boolean rule family. Mechanical guard rejects resurrected partial runner authority and reverse component dependency.

## Actual installed evidence

Noneditable `.installed/lib/python3.14/site-packages/agent_comms`, using unchanged reviewed native `fcadec01235eb0b5` at `/home/ts/wt/comms-selected-cold-compaction-native-20260928/stack/.pi-native-fcadec01235eb0b5/node_modules/@earendil-works/pi-coding-agent`. Only local HTTP provider responses/process termination are controlled; real package, pipes, tool sockets, files, SQLite, journals, claims and ACP runtime paths execute.

- `installed-native-v2.txt`: 4 native coding cases PASS before a migrated lifetime assertion failed: read/edit/write/bash, claimed-write, admission-floor cutover and triage→full. Exact receipt preserved, not called a wholly green suite.
- `installed-lifetime-final.txt`: **4 PASS46.56s**: saved direct refusal, triage refusal, triage→full refusal, transport EOF. UNKNOWN retained, children reaped/turn released, no old-input replay, genuinely new inputs complete.
- `installed-enrollment-final.txt`: **2 PASS6.96s**: real fresh enrollment→triage→full; existing unreviewed first-source CLI guard refuses before provider input and cannot replay. No safeguard weakened. `installed-fresh.txt` preserves the initial invalid positive expectation for that already-guarded CLI path.
- `installed-acp-final.txt`: **1 PASS26.34s**: real ACP saved histories, native B reply automatically considered by A, exact reply injection feedback, settled owners and guarded restart. Normal selected production entry path, zero external provider calls.
- `installed-final-exact-enrollment.txt`: final source enrollment/whole-snapshot-fence recheck.

## Focused and failed evidence

- `affected-closure-final.txt`:57PASS/1SKIP plus one old original-cohort fixture failure; includes real SQLite/registry/claimed-write/ACP preplan IPC and recovery. The skip is the existing prepared-native foreground package requirement, not counted as proof.
- `remaining-runtime-callers.txt`:11PASS13.52s: existing-turn refusal, crash/no replay, later claims, configured model, diagnostics, context and one-use lifetime.
- `caller-closure.txt`:23PASS and one stale assertion about an omitted tool keyword. Existing admission always passes `selected_tool_mode=None`; assertion migrated to the actual no-tools contract. Final affected receipt covers it.
- Initial focused receipts preserve stale publication monkeypatch and removed-runner-field callers; all migrated to actual publication/durable-record owners, never restored as aliases.
- Original-cohort two-process fixture originally let beta's NEW reply race alpha's passive receipt. Startup barrier diagnosis exposed legitimate reply triage in its full-only fake. The fixture now completes alpha's original passive observation before releasing beta; real automatic reply behavior is separately proved above. **foreground-original-cohort-final.txt:1PASS3.57s**, exact NO_WAKE/source/full-response assertions retained. Intermediate diagnostics retained.
- Known main history expectation remains unchanged: cursor includes published response at103 while old assertion expects102; already reported in378, Dalton owns it. No production or assertion workaround here.

## Integration

Parent owns merge/live package/paired install. PR381 branch and canonical package untouched. Parent main now includes381; this PR does not change native pins. No CI gate, no live/shared-root edits, no user input replay. Current whole-plan mapping updated in place; this scope does not claim all Q8/S14/T4 complete.
