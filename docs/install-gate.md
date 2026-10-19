# Two-machine install gate

Owner: `pi-install-gate`. The Claude installer worker owns `tools/upgrade_bus.py`.
Draft PR: https://github.com/OpenHCSDev/agent-comms/pull/1930. Do not merge yet.

## P255 structural repair

Current main was merged normally, preserving the published branch. This remains
an unfinished draft: passing local structural checks is not two-machine install
acceptance. P255 permits only `/home/ts/.cache/agent-scratch/pi-install-gate`,
with a 3 GB cap together with checkouts. The older HDD proof paths below identify
historical runs; they are not authorization to repeat those large copies now.
Current checkout plus small local logs are below 50 MB; no environment was built.

Before editing, the existing refactor-audit overlay read the production tools;
AST reads also covered FieldCodec, MroDispatch and Core's child-process owner.
All five relevant modules parsed under Python 3.14, with no omissions. Static
import resolution remains conservative for dynamic imports, as described below.

Owner changes across the complete gate family:
- External GitHub, TOML and installer answers become frozen records through
  FieldCodec. `ExternalRecord` only selects each record's declared external
  fields; it does not decode values, invent keys or tolerate missing fields.
- `SnapshotHome` is declared once in the standalone producer. `SnapshotCopy`
  inherits that record and owns placing it locally. The producer streams the
  route file unchanged; the client uses the existing ActiveRoute type.
- Core's `Platform` launch and `ParentedProcess` now own process groups,
  cancellation and stream retirement. The gate's `poll`/kill/optional-stream
  cleanup is deleted rather than renamed. Running/exited outcomes remain Core
  objects, not nullable fields in the gate.
- SourceReferences uses existing MroDispatch handlers for Python AST classes.
  Commands, protection-null states, machine outcomes and gate outcomes carry
  their own behavior; consumers no longer recover it from strings or mappings.
- Stdout and private stderr have separate owners. Installer stdout is decoded
  once, not searched repeatedly for possible JSON objects. Missing file/read
  results cannot pass solely because the child exits zero.
- Only named failure types are handled. Unexpected failures still write a
  failed result and post failure in `finally`, then propagate unchanged.
  Source coverage has explicit unchecked and checked records; an interrupted
  read is not reported as an empty set of affected paths.
- The child owner's asynchronous wait runs until completion. Its short bounded
  reap is only used after exit, so a real install is not accidentally cut off
  after two seconds. Storage policy is captured once per snapshot, not reread
  for every archive entry.

Before/after production evidence: StringSubscript 37 -> 0; FamilyFlattened
4 -> 0; TypeSwitch 1 (three arms) -> 0; NoneAbsence 3 -> 0;
FunctionLocalImport 3 -> 0; ForeignFieldSpellings 1 -> 0. BannedVocabulary,
StringIdentity, Ruff findings and mypy errors are also zero. No suppressions,
checker changes, edited baselines, casts or Any annotations were introduced.
The feature's batch Src-Budget declares its actual new production lines; it
waives no structural finding and uses the existing sum-of-budgets mechanism.

The requested `agent-comms-commander order pr P255 ...` command was attempted.
Core chooses `/home/ts/wt/pi-install-gate--P255`, which does not exist; the
retained branch is at `/home/ts/wt/pi-install-gate--P1`. It failed before PR
creation. No duplicate PR or fabricated order reference was created. #1930
continues updating in place through ordinary pushes.

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
again afterwards. If a writer opened WAL during that attempt, the immutable
bytes are discarded and one normal SQLite transaction is attempted; otherwise
the race fails closed. SQLite companions are not copied
independently. Regular files are read to captured lengths; sockets are skipped
and symlinks/special files refused. This is a per-store snapshot and file-prefix
cut, not an atomic transaction across all independently committed stores.

The installer is invoked as a client, never imported or rewritten. Its writes
are restricted to private builds/copies, Git metadata and uv's existing shared
package cache, not installed runtime packages; retained absolute session paths
cannot write the originals. Each copy retains its original position relative
to its synthetic home, so the gate itself does not hide default-root identity. The gate removes its own agent/turn
identity from the child's environment using the existing launch declarations.
It privately copies the configured npm cache: native preparation's pinned MCP
install deliberately runs offline, so a new HOME must not discard that input.
uv keeps using its existing cache instead of cloning the same dependencies
again at every checkpoint; lock resolution still upgrades the declared mains.
The previous executable is this machine's installed runtime. The unused
foreign-runtime field was deleted; a remote executable path is not assumed
runnable locally. The target revision supplies
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
- Verified: seven client/snapshot checks (31.6 s in the final run), including
  preserved default-root position inside its synthetic home, committed WAL data while a
  writer holds an uncommitted row, unchanged source-file hashes, closed WAL
  stores, denied writes, failed transport and symlink refusal.
- Verified on desktop: read-only bubblewrap can serialize its live SQLite WAL
  store. This is not a completed full desktop rehearsal.
- Verified on GitHub: required context `install-rehearsal`, strict=true,
  enforce_admins=true.
- Verified: the actual native MCP dependency-preparation function installs all
  95 packages offline using the private copy of the configured npm cache (5 s).
- Main proof, pinned at `b76b58e67`: red. Laptop reached native preparation but
  its initially empty npm cache rejected `zod-to-json-schema` with ENOTCACHED.
  The private-cache fix above was subsequently verified on the actual function.
  Desktop rejected a changing checkpointed projection store; a final normal
  SQLite read after that journal transition is now implemented, but the full
  two-machine proof has not been repeated after those corrections.
- Historical proof, `b5b8de987^` to `b5b8de987`: laptop reached native preparation
  and installed those 95 packages, then refused the native tree with
  `Native package tree differs from pinned artifact` under npm 11.18.0. It did
  not reach the writer guard. The command then hit its 300-second limit during
  the desktop copy (exit 124); the receipt is failed, never skipped or green.
  That red is not the requested regression proof. Private receipts:
  `receipt-b76b58e67493-1152620.json` and `receipt-b5b8de987a1c-1177018.json`
  under the owned scratch path below. The final client also preserves the
  home-relative bus position and reuses uv's cache; these have focused coverage,
  not a repeated passing full rehearsal.
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

Assumed: an operator reviews same-repository candidates before executing them
on private copies; the existing Claude installer worker owns the remaining
rehearsal-identity and native-build corrections.
