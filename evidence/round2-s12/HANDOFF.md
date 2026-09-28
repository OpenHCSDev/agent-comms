# S12 A13 foundation (surface still open)

No production table or stored data is changed by this foundation. It provides
one declaration owner for table and projection rows; SQLite conversion is owned
by nominal SqlStorage members using FieldCodec, and table membership/names use
DeclaredFamily. Connections and transactions remain with callers.

28 local checks passed (two new family/new-case behavioral tests plus existing
FieldCodec/DeclaredFamily checks). Tests use real SQLite files under the owned
worktree, including close/reopen and rollback. Initial test run failed because
one fixture nested BEGIN after a failed statement and one expected ValueError
where the existing codec raises TypeError for missing dataclass fields; corrected
fixtures preserve rejection and transaction behavior. Both receipts retained.

No mocks, provider calls, live sessions, restarts or installation involved.
Full source guards and affected installed path acceptance remain part of the
consumer migrations; this is not a claim that S12 is completed.

## First production reader adoption

`optional_awareness_projection.py` now declares its join projections and routes
all SQL reads through TypedRow. Removed raw column/tuple extraction and redundant
primitive checks. Scope guard discovers modules importing typed_table, rejecting
raw fetch/row iteration and handwritten table DDL or writes without exceptions.
25 checks pass (A13, guard family and 21 awareness behaviors), Ruff passes.
The shared coordinator schema validators still require sqlite3.Row; the connection
keeps that documented dependency until their own S12 migration. Removing it broke
12 behavior checks; restoring the required connection contract fixed them without
weakening assertions. Failed and corrected receipts retained.

This adoption changes no persisted schema, hence no reset/highwater movement.
A reset of coordination/candidate/native runtime state must occur only at the
parent's quiet cutover: capture the existing checkpoint source highwater H, retain
original history/UNKNOWN evidence, and initialize existing candidate/current owner
admission cursors so all native wake selection is strictly > H. A cursor reset to
zero is not an acceptable install. Parent owns the actual D22 cutover and proof
that old addressed rows cannot create new attempts. No migration store is added.

#230 remains the draft owner for all S12 work. NativeRuntimeInput and table
migrations are still open; foundation adoption alone is not full surface closure.

## Admission-floor handoff (source evidence; parent L0b owns activation)

The existing candidate checkpoint (`wake_candidates.sqlite3`, checkpoint row:
root_id/device/inode/byte_offset/tail_digest/last_seq) records index progress,
NOT eligibility. Rebuilding it indexes historical recipients too. Existing
`cohort_foreground._accept_visible_initials` accepts every addressed initial after
its supplied cursor; `run_foreground_once` starts that cursor at zero.
`CoordinatedPrivateRunner._select` uses `self.after_seq`, also defaulting to zero.
`_production_optional_awareness` and `native_source_cursor._bounded_coverage_pages`
begin their ranges at zero. Therefore a reset plus checkpoint rebuild alone
would make historical inputs eligible again or stall fresh proof coverage.

Concrete proposal for parent review/implementation: persist `admission_after_seq`
on existing WireMetadata during the quiescent D22 rewrite, with H captured under
its existing bus lock and checked against last_seq. Preserve this field through
all future publications/checkpoint updates. This is the current root's authority,
not a migration sidecar. Missing/new-format-invalid authority must fail closed.
All claim acceptance and native selection use max(caller_after_seq, that floor),
including direct accept_initial_cohort calls, not only the foreground scanner.
Optional awareness and bounded native source coverage must start at the same
floor; original messages remain available through history reads. The candidate
index can be rebuilt for complete history and then page strictly above H for
wake eligibility. Checkpoint through_seq/byte_offset still attest the rewritten
source; they are not fabricated native inputs.

Do NOT seed CurrentNativeCursor.covered_seq or injected_seq to H: those are native
proof projections. Initial native proof is empty; only post-floor verified input
may advance it. Admission floor does not assert that old UNKNOWN work succeeded.
Required cutover test: retain an old addressed unfinished/UNKNOWN input <= H;
reset and relaunch; verify zero new claim/native/provider attempts for it, then
append one addressed input > H and verify its single normal admission. Repeat
ordinary restart/index rebuild to prove the floor survives; history retains both.
This is an explicit remaining integration requirement, not completed behavior.
