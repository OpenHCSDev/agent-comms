# Shared cold-restored native context admission

## Cause and code

`SessionContext.restore` used summary-request serialized-byte allocation to decide
whether the selected native token context fits. `acSelectedCompactionSettings`
then trusted that false CompactionContext and triggered an owner summary, even
when native usage was under the selected model minus its effective reserve.
This affects ordinary cold restores and forks. It does not require a large file.

The actual retained child branch measured 145167 tokens / 272000 window, with
16384 reserve; its 921607 serialized bytes exceeded the unrelated 191712 summary
allocation. User-reported earlier24%/35% UI readings are historical observations;
we do not claim they described the later captured leaf. Neither load warrants
that byte-triggered summary.

The fix uses AgentSession.getContextUsage, the selected model and SettingsManager.
After a compaction, native usage is intentionally unknown until a fresh response:
only then do we estimate the current selected context with the native token
estimator, excluding stale pre-compaction usage. Summary allocation is unchanged.
No global disable, enlarged cap, history rewrite or input replay.

## Evidence

- Actual installed native SDK plus loopback HTTP: fork24%, fork35%, ordinary
  cold-restore63% each answers once, zero compaction, one request. Physical native
  histories are over342k bytes, so the prior byte gate rejects them. These native
  fixtures explicitly seed the prior assistant usage; independent Dalton338 owns
  the full provider-created parent -> production fork -> attached ACP regression.
- Old native ordinary63% RED at the false requiresCompaction assertion. An earlier
  path setup error is separately retained and is not the causal RED.
- Existing actual native whole/split compaction generation+commit+reopen remains
  passing;3 total loopback requests,0 paid calls.
- Installed Python native preparation2 passed; full input fences28 passed with13
  native opt-in skips in the earlier focused run, not a full native suite claim.
- Complete copied candidate verified by native_package.py; manifest pins exactly
  this native implementation. No live package was changed.

## Boundaries and pending independent work

This urgent slice can install independently from339, whose fork creation snapshot
and startup-input caller work remains separate. ContextUnavailable display on
cold ACP load is not established fixed by these tests; no blanket UI claim.
Existing UNKNOWN attempts stay preserved. Parent owns live installation.

Ownership: IDEN-2 (bytes are not tokens), IMPL-5 (reuse native usage authority),
TIME-9 (no alternate codec), IMPL-10 (Ready/Compaction context keeps its existing
state behavior). Both latest22:16 NRA and refactor-audit skills reread.
