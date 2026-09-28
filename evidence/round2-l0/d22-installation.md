# D22 installation checkpoint

The one-shot installer atomically exchanges a prepared current root with the
original directory, retaining that original directory as the rollback backup.
It refuses registered live owners and identifiable attached clients; it neither
stops them nor submits/replays inputs. Uses the actual .bus.jsonl.lock during
exchange, releases the new inode lock before current readers reopen, and retains
original directory contents until verification succeeds. Converted archive paths
remain in place and their snapshot revisions are refreshed.

Actual complete live-root copy acceptance passed with135currentmessages,
8420archivedmessages,104threads,13UNKNOWNinputdispositions and301originalbackup
files. All original backup contents compared; native sessions and diagnostics
preserved. Current converted schemas and every SQLite row are compared by streaming
ordered rows; retained JSON documents and full wire agree with the candidate.
Human bus read membership and an explicit internal-session inode read position
survive the directory exchange. Owned disposable copies were removed.

Compaction253 converter is integrated: preserve current compaction journal,
including actual97UNKNOWNraw IDs and one refused-summary barrier. Goal attempt
runtime is preserved offline and removed from goal-private/goal_attempts.sqlite3;
no old ready digest/launch grant or native process binding is recreated. Top-level
obsolete goal_attempts, if present, receives the same offline disposition. The
actual active root currently has no nested goal-private database; the old shared
root inventory is separate and remains untouched.

SQLite sidecars are removed before each converted database is installed. Current
checkpoint is rebuilt with existing owner; exact marker access/root/floor checked.
Archive sources and root/history manifest revisions rechecked before exchange.
History conversion receipts and old runtime evidence are installed under
precutover-evidence, in addition to the untouched original directory backup.

Reproduce in the parent owned tree (psutil is a disposable operator dependency):

    PYTHONPATH="$PWD/src:$PWD/tools/cutover" timeout 60 .venv/bin/python -u evidence/round2-l0/exercise_root_install.py

Source/shared/live installations were not modified.

Failures corrected before this checkpoint: missing operator psutil dependency;
protected unrelated systemd environ access; old SQLite sidecars; omitted nested
goal runtime path; transcript inode binding; archive/floor validation and backup
destination fsync. Review found compaction journals cannot be treated as disposable
runtime authority because absence removes unresolved send barriers. They now use
253's current declaration conversion. Initial scalar-only rehearsal receipts are
superseded and cannot install through the new source-revision receipt.

This is copied-root operator acceptance, not a completed live cutover. Native243,
combined248 and pairedToad107 integration/installed acceptance remain. Execute
only at the quiet cutover; remove the one-shot tools after real installation.
