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
