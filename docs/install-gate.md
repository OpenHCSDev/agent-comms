# Two-machine install gate

Owner: `pi-install-gate`. Installer implementation remains with the Claude installer worker.

The local gate snapshots the laptop bus and the desktop bus, invokes the target
`tools/upgrade_bus.py --rehearse` on local copies, and reports the exact PR head
through the `install-rehearsal` commit status. Both machines are mandatory.
Private data and detailed output stay on the laptop; GitHub receives only outcomes.

The snapshot producer runs inside bubblewrap with `/` read-only, no writable
mounts, no network and bytecode disabled. It streams tar to this machine. SQLite
stores are opened with `mode=ro`, read in a transaction and serialized from the
SQLite pager (including WAL), not copied as database/WAL pairs. Regular files
are read to their captured length. Sockets are not data; symlinks are rejected
rather than allowing rehearsal to write outside the copy. This is a per-store
snapshot, not an atomic transaction across every independently committed store.
A failed SSH connection or unsupported read-only SQLite snapshot is a failure.

Affected paths come from the distribution's wheel/build declarations and the
installer's local imports and repository-path references, at both base and head.
This deliberately covers the whole declared runtime distribution, rather than
trying to guess whether a changed line writes storage. No separate path glob
registry is maintained. Parse errors fail closed.

Implementation/proof checklist:
- [ ] Source-derived affected paths, including deleted/base-only paths
- [ ] Read-only snapshots from both machines
- [ ] Target installer client, with bounded processes and private local receipts
- [ ] Exact-head GitHub status and required merge check
- [ ] Main: both actual rehearsals pass
- [ ] b5b8de987 parent + commit: laptop rehearsal fails on writer guard

Disposable output owner: `pi-install-gate`. Use a private run directory under
`/home/ts/mount/hdd/agent-scratch/pi-install-gate` for large snapshots: the default
home volume has only 26 GiB free and the desktop bus is already 26 GiB. Remove
copies after each run, retaining only concise local receipts. Preserve original
buses, sessions and uncertain inputs. Never merge this PR from this task.
