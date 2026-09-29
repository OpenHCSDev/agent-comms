# Q8 selected execution — implementation in progress

Deletes the partially initialized SelectedExecution runner (lease/progress/native input, model, prompt, tool and enrollment scratch fields) and its three ACP write callbacks. Selected participant owns the exact lexical lease and frozen recipient; session enrollment owns and consumes first-start capability; an engaged attempt always owns FullNativeSend/DurableTurn; a reserved SelectedRequest owns native failure reporting. Existing stage declarations own UNKNOWN settlement and nominal decoded triage owns its atomic disposition. Existing coding/claimed-write owners still perform the actual effects. No new storage, codec, native package or send authority.

Whole callers are being migrated, including stale fault-injection sites. First focused run: 38 passed; two retired publication monkeypatch callers found and migrated. Separate known main failure (PR378 receipt already reports it): auxiliary cursor includes a committed response at 103 while old expectation says102. No assertion weakened; Dalton owns history.

Actual installed native acceptance and final guards pending. This draft is NOT ready/live. Parent owns paired integration and installation. PR381 branch/package untouched.

Patterns: IDEN-1 uses RegistryOwner/ParticipantOwner and complete FrozenRecipient/WakeDecision values; IDEN-3/IMPL-10 removes optional runtime phase bundles; IMPL-4/5 completes instruction/tool/effect behavior on SelectedAction; BOUND-1/TIME-9 decode triage once with canonical FieldCodec; IMPL-12 retires ACP callback copies. Latest archive-aligned skills reread; no anonymous per-boolean rule list.
