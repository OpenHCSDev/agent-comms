# Selected native dispatch ownership (S14)

Deletes the five runner implementations `_reserve_triage_input`, `_reserve_full_input`, `_verify_native`, `_record_triage_result`, `_record_full_result` and their callers. Production checkpoint: 281 lines deleted, 288 added across three existing owners; no new modules, stores, codecs, state records, aliases or parallel dispatch.

## Ownership

- `NativeSendStage` shares durable input reservation, current participant/input ownership and source-binding verification. Existing triage/full subclasses own their distinct claim reservation and settlement.
- `NativeRuntimeInput` owns unproven capability checks, exact live-context corroboration and the single CAS committing the complete native proof.
- Existing `NativeIdentityCheck`, `NativeBindingCheck`, `ParticipantOwner`, `OwnerFence`, `TextDigest` are reused. No new validation-rule-per-field family.
- Full settlement consumes the existing engaged assignment's exact-target capability; triage IGNORE keeps both SQL edges in the same transaction as its proof.
- Runner no longer validates decoded RPC primitives or duplicates eight-field identity tuples. The native RPC decoder/live executor and proof-journal decoder retain validation; disk bytes alone grant no admission/recovery authority.

Patterns: IMPL-4/5/12 (complete existing stages/shared behavior); IDEN-1/3 (existing ownership and row capability); BOUND-1 (trust decoded live native evidence). Exact current archive SKILL/catalog and NRA method reread. Resource restriction: no large NRA scan or new agents.

## Evidence so far

- Focused affected durable-store checks: **13 passed, 50 deselected, 5.80s**, `focused-first.log`. Includes triage/full crash-no-replay, forged evidence, generation revocation, ambiguity, terminal failure, exact retained inputs, one-shot runner.
- This is source/unit boundary evidence, not actual native acceptance. Actual pinned native local-provider acceptance is next; readiness remains draft.
- Branch includes normal merge of main `b1c0bde6` and Boyle350 `2a2a1ad5`; no edits to his admission/response/shared-identity files. Parent owns351/350 merges and live deployment.
