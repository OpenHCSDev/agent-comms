# S2/T4: native reusable lifecycle

Base: integrated #322 `fe76c50e`. Owner: native backend sidecar. Parent owns deployment.

Reread the corrected 22:16 refactor-audit install, both skills, pattern README,
IMPL-14 and `scripts/audit/chain_terms.py`. Classification: persistent nullable
custody is IDEN-3; retention's scattered flags are IMPL-10; comparing native
identity parts separately is IDEN-1. These are runtime state, not stored formats.
Pi RPC and native saved history remain external contracts.

## Implementation scope

Replace PersistentPiSession's nullable child/reader/error-task/session/revision
bundle with lifecycle states that own the actual child and retained proof.
Retirement owns its shielded task and required reopen; cancellation cannot lose
its process handle or waive strict reopen. Private RPC borrows the same retained
state instead of repeating nullable-field validation. Turn retention follows
observed native input lifecycle and existing output/input/stats facts.

Delete `reusable`, `can_retain`, public nullable custody fields, duplicate private
idle checks, copied turn admission flags and field-by-field identity checks.
No aliases, default compatibility paths, per-boolean rules, or parallel registry.

Crossings: Boyle owns #323 owned_turn/turn_runner/progress; Carver owns Toad.
Neither surface is edited here. Two existing compaction preparation call sites
must change their old `persistent.proc is None` query to owned availability;
these surgical changes will be explicitly identified for #319 integration.

## Acceptance

Actual pinned native reuse, auth/session-change retirement, cancelled retirement,
strict reopen, cold compaction preparation and selected summary/commit. Preserve
no replay, exact input binding and uncertain-outcome refusal. Local loopback only.
Touched-file chain terms and independent class-size ratchets may not increase.
No live restart or installation by this sidecar.

Status: implementation in progress; no new verification claimed.
