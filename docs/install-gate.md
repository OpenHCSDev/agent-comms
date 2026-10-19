# Two-machine install gate

Owner: `pi-install-gate`. The Claude installer worker owns `tools/upgrade_bus.py`.
Draft PR: https://github.com/OpenHCSDev/agent-comms/pull/1930. Do not merge yet.

## Run locally

Use the installed Core interpreter, not a Python without Core's dependencies:

```sh
python tools/install_gate.py --pr NUMBER --scratch /home/ts/mount/hdd/agent-scratch/pi-install-gate
```

This posts `install-rehearsal` pending, then success/failure for the exact PR
head. An unaffected PR gets success without copying private data. An affected
PR must complete both machines. Failed SSH, snapshot, build, skipped rehearsal,
CLI read or interruption is not success. Logs and copies are never uploaded.

Main now requires `install-rehearsal`, an up-to-date branch and passing status,
including for admins. Existing protection settings are preserved when adding
this context to an already protected branch. `--require-status` is the explicit
admin setup command; the actual GitHub protection was read back after setup.
No hosted workflow has access to these buses. Same-repository PRs only: an
operator must review a candidate before running its code on private copies.

For a forced local proof, without posting a status:

```sh
python -X faulthandler tools/install_gate.py --revision origin/main --base origin/main^ --scratch /home/ts/mount/hdd/agent-scratch/pi-install-gate
python -X faulthandler tools/install_gate.py --revision b5b8de987 --base b5b8de987^ --scratch /home/ts/mount/hdd/agent-scratch/pi-install-gate
```

## Owners and source coverage

The wheel package roots, force-includes, custom build hook and Git submodule
manifest define the distributed source. The installer's local import closure
and repository-path literals add its build inputs and carry directory. This
reaches `NativeBuildInputs.paths`' existing source families, including native
experiments and extensions. Base and head inventories are unioned; deleted
files still count. No separate runtime/storage path glob list is maintained.
Parse errors fail closed. The inventory is conservative: an entire declared
source directory counts even if only some files are read on this install.

AST evidence: the installer imports `cutover.carry`; `CarryChain` declares
`tools/cutover`; the runtime imports `NativeBuildInputs`, whose `paths` declares
the native build families. `pyproject.toml` owns wheel roots and build hooks.
The gate consumes those declarations without changing or copying their lists.
Literal paths/imports are resolved statically. Dynamic imports beyond those
are not claimed resolved; the whole distribution and declared directories
remain covered. No parse omissions were observed with Python 3.14.

## Snapshot and execution safety

The same `tools/install_snapshot.py` is streamed locally and over SSH. Each
producer runs inside bubblewrap with `/` read-only, no writable mounts, no
network and bytecode disabled. Nothing, including SQLite SHM or a temp file,
is written on the desktop. The transport is a zstd tar stream into a private
local directory.

SQLite opens `mode=ro`, starts a read transaction and serializes through its
pager, including committed WAL pages. Closed WAL-mode stores sometimes cannot
open without creating SHM; only when there is no WAL or journal may they use
`immutable=1`, with unchanged inode/size/mtime and absent companions checked
again afterwards. A race fails the snapshot. SQLite companions are not copied
independently. Regular files are read to captured lengths; sockets are skipped
and symlinks/special files refused. This is a per-store snapshot and file-prefix
cut, not an atomic transaction across all independently committed stores.

The installer is invoked as a client, never imported or rewritten. Its writes
are restricted to private builds/copies and Git metadata; retained absolute
session paths cannot write the originals. The gate removes its own agent/turn
identity from the child's environment using the existing launch declarations.
The previous executable is this machine's installed runtime; foreign runtime
paths are recorded, not assumed runnable locally. The target revision supplies
the installer and runtime being checked. A zero exit without an actual carry,
file-comparison and CLI-read receipt is rejected.

Worktrees, build homes and TMPDIR remain under `/home/ts/wt`. The desktop copy
is 26 GiB; home has insufficient space for it plus the installer's second copy.
Use the HDD scratch path above. Capture reserves the second copy and the
existing storage owner's configured free-space floor. Copies are removed on
success, failure or interruption; only small private logs/receipts remain.

## Verification and remaining blockers

- Implemented: source-derived affected paths, both read-only producers, private
  installer execution, exact-head status, refusal of skipped rehearsals.
- Verified: six client/snapshot checks, including committed WAL data while a
  writer holds an uncommitted row, unchanged source-file hashes, closed WAL
  stores, denied writes, failed transport and symlink refusal.
- Verified on desktop: read-only bubblewrap can serialize its live SQLite WAL
  store. This is not a completed full desktop rehearsal.
- Verified on GitHub: required context `install-rehearsal`, strict=true,
  enforce_admins=true.
- Unfinished: both main rehearsals passing and the historical writer-guard
  failure at the actual guarded write. A different red does not prove this.

Concrete installer gap: `rehearsal_home()` only rebinds an existing active-route
file. With no active-route file, the default-root relationship is lost when it
puts the copied bus beside, rather than inside, its synthetic home. The writer
guard can therefore miss it. That correction belongs to the existing installer
worker, not a competing gate-side implementation. The target's installer also
builds/copies/hashes the full 26 GiB desktop bus; the requested 300-second check
budget may expire before that finishes. Acceptance must record that honestly.

Private disposable output owner: `pi-install-gate`, at
`/home/ts/mount/hdd/agent-scratch/pi-install-gate`. Keep concise receipts; remove
large copies/builds when their run ends. Preserve original buses, sessions,
credentials and uncertain inputs. No runtime was published or restarted.
