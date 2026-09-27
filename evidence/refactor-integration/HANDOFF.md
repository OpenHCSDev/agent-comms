# Combined refactor integration — 2026-09-27

Parent integration in persistent ~/wt/comms-refactor-integration-20260927.
Normal merges preserve the complete branches for PR125, PR129, PR127, PR134,
PR138 and PR137 on main after PR95 and the restored-subscriber repair PR139.
No completed worker branch or shared checkout was edited.

Resolved integration boundaries:
- LockedStore's shared reads coexist with native inherited descriptor locks;
  _store_lock still yields its descriptor and POSIX release remains last-close.
- S4 read-ledger paths and S8 GoalWaits.filename both remain authoritative.
- Goal and read-policy imports compose; newer private restoration, native
  compaction/admission, failure recovery and default enablement remain present.
- Existing export tests caught S4's strict Message serializer rejecting legacy
  nonfinite timestamps. MessageWireCodec extends the existing FieldCodec for
  historical message floats; the shared document codec remains strict. Existing
  export policies retain their invalid-time counting/filtering/presentation.

Local combined-tree results so far:
- Foundation/store/export/relationships: 148 passed after compatibility repair.
- Goals/read/routing/roster: 73 passed.
- Events/runtime: 67 passed, 11 optional native cases skipped in this selection.
- Goal ACP/owner/attempts: 131 passed.
- Message/resource/envelope boundaries: 155 passed, 1 inapplicable skipped.
- Mounted Toad PR77 against this combined core: partial-paint and DM-rebind
  pilots pass. These execute the real Textual widget paths.
- Native compaction selection first exceeded its 60-second outer bound. Bounded
  complete partitions then passed: 7 summary cases plus 2 inapplicable skipped;
  10 decline/settings/no-goal cases plus 2 inapplicable skipped; 49 native owner,
  admission and actual packaged-tool loader cases.
- 16 inherited authority/parent-death tests pass on the combined shared/exclusive
  lock implementation.
- Installed core and Toad wheels in runtime-refactors-20260927 passed both
  partial-paint and DM-rebind pilots with PYTHONPATH unset. Imports resolve to
  site-packages. The installed native package preflight passed against the
  current root; roster103 and bus17 remain present. Constructing the new Comms
  reader creates S4 read_ledger.json through its normal one-time migration; old
  read-marker files remain intact. No executable owner was restarted by this check.

Logs including failures remain under .artifacts/integration. CI is deferred.
This includes isolated installed-wheel validation, not yet live owner activation. Toad PR77
must ship with S4 core; coupled core/Toad pins and stack lock will be refreshed
before activating the combined installed runtime. History migration, normal
channel coding tools and real-provider compaction retention remain unfinished.
