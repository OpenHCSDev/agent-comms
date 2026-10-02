# Passive failed-turn observation pilot

Scope: observation only. No grants, PLAN_READY, retry endpoint, owner decision,
input disposition, goal transition, scheduling, or dispatch changes. The existing
ACP failure callback remains responsible for failing the original attempt.
This pilot does not expose a UI or RPC endpoint and is not installed/live.

## Canonical evidence

GoalAttemptStore schema 5 adds one `failed_turn_observations` table. Its primary
key/FK is the canonical attempt ID; turn ID is also unique. A row contains the
original goal ID/revision/generation, owner name/incarnation/worktree, admission,
turn generation/ID, and allowlisted backend terminal reason. No token, provider
text, prompt, input bodies, or reconstructed historical turn identity is stored.

Only the existing ACP callback with a claimed launch permit supplies evidence.
Its captured goal/owner/turn claim and terminal registry snapshot must agree on
owner incarnation, worktree, process, admission, goal identity, and exact active
or just-finished turn generation/ID. A newer goal revision from the same in-flight
turn (including an owner pause) does not erase the original revision witness.
Mismatched binding is omitted; stale reservations still raise the existing
StaleAttempt error. Unclaimed prelaunch failures do not acquire a backend row.
Successful backend terminal results without verified goal progress do not mint
backend-failure observations.

The row is inserted inside the existing `record_failed` transaction, after its
original failed/blocked updates. An observation-only insert error is isolated
with a savepoint: missing observation is preferable to losing the failure fence.
An error that aborts the entire transaction follows existing StorageUncertain
handling and ACP's existing finally-path unobserved failure recording. A crash
before commit leaves a reserved/claimed unresolved attempt, never READY; a crash
after commit leaves both failed/blocked state and observation. Storage uncertainty
is not a success receipt. Duplicate callbacks retain existing StaleAttempt
behavior and never rewrite or duplicate the original row.

Migration from v2/v3/v4 is additive to existing authority tables and does NOT
backfill historical records. Old binaries cannot initialize a v5 store; this is
not a mixed-version rollout or an instruction to mutate installed stores.

## Read-only redacted presentation

`read_failed_turn_projection` opens the goal-private database with `mode=ro` and
`query_only`, refuses WAL/nonregular/invalid/missing stores before opening, and
never constructs GoalAttemptStore or migrates/repairs anything. It reuses only
the privacy/preflight pattern from recovery_projection, not that separate
coordinator schema's reader, retry fields, or owner identity.

Caller must supply trusted canonical registry/admission/status and pause-event
snapshots. This is NOT an authenticator or an atomic cross-store revision. Its
result must never be used as dispatch authority or proof that an owner has not
paused. Stopped/replaced/changed-admission/newer-turn owners are unavailable.
Historical failures without exact binding are unavailable, not guessed from
name, time, diagnostic prose, exit code, or input receipts.

Only `schema`, `state`, and a bounded reason are projected. States are
`backend_suspended`, `owner_paused`, `paused_uncertain`, and `unavailable`.
`backend_suspended` means the private generation remains blocked after a bound
failed backend turn. It does NOT mean recoverable, retryable, or runnable.
Missing/stale/model/runtime pause attribution is not proof of no owner pause;
a paused public goal with such attribution projects paused_uncertain. No
canRetry, secret/incident/turn IDs, raw diagnostic, filesystem path, publication
key, or control action crosses the projection boundary.

Existing UNKNOWN/STARTED receipts remain unchanged and are not interpreted as
replay rights. Owner pause storage and public goal behavior are inherited from
PR100, integrated normally; the pilot does not modify them.

## Tests

The provider-free tests exercise exact/mismatched/stale bindings, duplicate
callbacks and restarts, migration without authority-row changes, withheld
prelaunch observations, stopped/replaced owners, all pause-attribution cases,
read-only missing/invalid/old-schema/WAL stores, insertion failure, transaction
rollback, commit/fsync failure, and actual child-process crashes before/after
commit. ACP integration checks failed-done (including exit zero), EOF, owner
pause, insertion/transaction-abort failures, exact diagnostic turn correlation,
byte-identical old UNKNOWN/STARTED ledger and ACK cursors, no scheduling, and
continued ordinary-resume rejection. No native/provider acceptance is claimed.

Future recovery remains separate: trusted owner-control ingress, exact scope and
revocation binding, domain-specific disjoint effect proof, and consumed-before-
send crash fencing require independent review. PID-owned local JSON retry_goal
is not authenticated owner-click provenance and is not used here.
