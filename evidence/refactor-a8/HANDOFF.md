# A8 durable document ownership

Branch: `codex/refactor-a8-locked-store-20260927`.
Starting main: `e4cd10e` (PR126). Owner: this A8 implementation worker.
Scope expanded by the dispatch README during this run to GoalPauses and
GoalHistory where applicable. Parent/S1/PR95 files were not edited.

## Implementation and integration

`LockedStore` is the public ABC owning shared reads, exclusive read/modify/write,
FieldCodec conversion, mode-preserving atomic publication, file fsync and POSIX
parent fsync. GoalWaits and GoalPauseEvents inherit that algorithm; their old
JSON/locking/writing algorithms are removed. Their existing path constructors,
snapshot/record/clear APIs, durable JSON shapes, enum values, tuple encoding and
legacy defaults remain. GoalWaits still ignores extra wait-row keys, using its
dataclass fields to derive the projection. Malformed primitive fields now fail
at the shared typed boundary instead of entering goal runtime code.

GoalHistory is a SQLite intent/commit journal, not a whole-document store. It
retains its registry-lock ordering, SQL transactions, reconciliation and fsync;
its goal JSON encode/decode now derives from FieldCodec. Replacing its database
with LockedStore would change its durable shape and journal semantics.

The lock extension is confined to `declarations._store_lock`: `shared=False`
remains the default; `shared=True` selects a read lock. The canonical lock path,
nonblocking option, bus read guard and descriptor lifetime are retained. The
new document algorithm uses the existing `_replace_snapshot` retry owner. The
existing `_atomic_write_text` cannot retain prior bytes on parent-fsync failure
or preserve a previous file mode; it remains untouched for other owners. These
stronger document guarantees belong to LockedStore, not to each adopter.

Both existing foundation commits were cherry-picked unchanged:

- `19323fb3ff421fc94b81f419932d71f57ad01523` -> `04994bd`.
- `c4c4d578b049e8527781d033d04304c4ea46f086` -> `9cb6651`.

Dependency: draft PR125, foundation A1/A2 only. No S6/exporting implementation
was imported. The draft targets main and explicitly lists that dependency;
using the S6 branch as base would imply ancestry this branch does not have.
Once foundation lands, drop its duplicate cherry-picks during adoption. The
latest fetched main `0309f7b` adds only parent runtime/test changes; no owned file
overlaps. No merge, runtime restart, deployment or CI wait was performed.

## Lock closure traced before adoption

GoalWaits production entry points:

- `Comms._thread_views_for`, `goal_wait`, `_goal_snapshot`, `list_threads`: take
  a store snapshot after separate registry reads; some callers already hold
  wire. No caller holds the GoalWaits file lock.
- `recover_closed_goal_wait`, `release_waits_after_terminal_turn`, `update_goal`:
  wire encloses registry/bus work and sequential wait snapshots/record/clear.
- `consume_goal_wait`: called under the send-boundary wire lock, then clears
  the distinct GoalWaits document. `goal_snapshot` adds the outer wire lock.
- `stage_selected_participants` in supervised_cutover reads the legacy wait
  document, then records migrated waits under the private root's wire lock.
- `revision` and wire_watch only observe filename revisions; they do not read
  or write a GoalWaits snapshot. Their filename literals remain outside scope.

Before conversion, record/clear held the goal lock and called the unlocked
snapshot. After conversion, update owns the exclusive lock and calls only
`_read_unlocked`; snapshot uses shared read. No different descriptor recursively
acquires that same flock. There were no external `_write` callers.

GoalPauseEvents callers are `Comms.goal_pause`, `list_threads`, and the edit/
pause paths of `update_goal`. Wire may be held, but the pause-document lock is
never held externally. Its former record -> snapshot nesting is removed too.

GoalHistoryStore callers are `ThreadRegistry.register` and `goal_history`, under
the registry lock, optionally inside wire. History reads may reconcile writes,
so they retain the existing exclusive registry lock and SQLite transaction.

## Evidence and reproduction

All test processes are local, bounded, sequential shards. Pytest's repository
parallel/coverage defaults are overridden with `-o addopts=''`. Disposable test
roots, TMPDIR, NRA and mypy caches were under `.artifacts/` in this worktree.
No models or external agents were invoked. Available RAM stayed above 8 GiB
(observed 14–15 GiB); disk headroom was about 42 GiB.

Test environment: Python 3.11, existing shared test dependencies read-only, local
`metaclass-registry` source at `2d99d9ab79c06b1fb3c3583aae7360663e1cadff`.
A disposable venv used `.pth` references to those existing dependencies so the
production-only subprocess test could reset PYTHONPATH and still import the
foundation dependency. No shared installation changed and no download occurred.
With project/dev dependencies installed, reproduce each shard with:

```
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 TMPDIR=<owned-temp> timeout 60 python -m pytest \
  -o addopts='' -p no:cacheprovider --basetemp=<owned-test-root> <files> -q
```

- `test_locked_store.py test_declared_family.py test_field_codec.py`: 37 passed.
- `test_goal_standby.py test_goal_standby_liveness.py test_goal_input_review.py
  test_goal_owner_pause.py test_goal_history.py test_goal_mentions.py`: 65 passed.
- Integration shard: `test_supervised_cutover.py test_private_bus_checkpoint.py
  test_maintenance_barrier.py test_detached_owner.py`: 54 passed (28.71 seconds).
  Total final focused results: 156 passed. See the three result logs.
- Ruff on all five implementation/test files passed. Black with py311 passed.
  Mypy with `--follow-imports=silent` passed on the four owned store modules.
  `git diff --check` passed. No whole-repository typecheck or full-suite claim.

The dedicated tests use spawned processes with bounded waits and cleanup to
prove simultaneous readers, excluded writers, blocked snapshots, concurrent
writer preservation and wire/store nesting. Golden tests pin both document
formats and sorted history JSON. Fault injection covers file fsync, replace,
parent fsync, callback and encoding failure, for existing and missing files;
prior bytes/modes survive and temporary files are removed. Existing goal tests
exercise UNKNOWN handling, standby release, owner pauses and history crash cuts.

An intermediate compatibility-hook implementation hit Python 3.11 slotted
dataclass zero-argument super; explicit final-class super fixed it. An initial
integration run had 53 passes and one dependency-path subprocess failure; the
local test environment fixed that. Failure evidence is retained separately.

## NRA and semantic ownership limits

NRA checkout: `52fe8b4666a20583f0ddf8ed3b7a9e89857e4809` at
`/home/ts/code/projects/nominal-refactor-advisor`. Read its real CLI help,
getting_started, codemod catalogue/public API and nominal architecture playbook;
inspected PatchTargetOperation, SourceTextPatch and SourceReprovedOperation.
Exact-target patch preflight establishes source selection/revision, not fsync,
flock or callback equivalence. This change is authored implementation, not a
claimed NRA-native behavioral proof or automatic codemod replay.

The complete package-context scan was attempted with one parse/analysis worker,
4 GiB virtual-memory cap and 60-second outer timeout; it exited 124 without a
completed report (its partial JSON reports a parse deadline). Final local scan
covers locked_store, goal_waits, goal_pauses
and goal_history only, with `--no-auto-context-root --json-payload loop`:
`exact_compact_global`, 79 analyzed, 0 omitted, 0 findings, cache miss. That mode
label does **not** make its four-file context a global package audit. Raw output
is retained; no before/after package-wide reduction is claimed.

Relevant OOPSLA manuscript read:
`papers/docs/papers/paper1_typing_discipline/markdown/paper1_jsait.md`, notably
implementation reach, replica completion, and separation of ancestry from MRO.
Required persistence behavior now reaches both document owners by inheritance;
there is one determining lock/write algorithm and field serialization derives
from record declarations. A new valid record field no longer requires edits to
GoalWaits' hand-written decoder. No second registry or codec was introduced.
The paper's ancestry model does not prove filesystem failure behavior; tests do.

## Remaining boundaries

No S4/S5/S7/S8 lifecycle migration, filename-consumer migration, registry store
conversion, wire conversion, PR95 integration, Windows execution, installed-wheel
test, CI/full-suite gate or live-runtime validation is claimed. Windows retains
its platform lock API and no-directory-fsync convention; this run proves POSIX.
Atomic replacement requires same-filesystem hard-link support for rollback.
Process death can leave staging/recovery names and exposes ordinary old-or-new
atomic state. Repeated I/O failure during rollback cannot guarantee restoration;
if restoration itself fails, the prior inode is retained at `.previous` for
recovery. This is a document store, not a new recovery journal.

Next adopter: install/reuse foundation, take the A8 implementation commit, run
these focused shards against the integration tree, and keep the existing
wire -> registry/document ordering. Do not call a store's public read/update
from inside its own callback or under its already-held canonical file lock.
