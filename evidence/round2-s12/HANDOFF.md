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
