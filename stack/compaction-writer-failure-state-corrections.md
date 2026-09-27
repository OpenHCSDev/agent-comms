# Caught-error native manager corrections — exact successor, review pending

The independent `9c7445e` review found two ordinary caught-error continuations:
branch destination-lock denial left a nonexistent destination with `flushed=true`,
so the next append created headerless JSONL; malformed `setSessionFile` bound the
new target revision while retaining the old tree, so the next append extended
its corrupt tail. Both also reproduced against the canonical `8cf666c` package.
The original report/probes remain unchanged:

- `/var/tmp/ac-pr48-writer-coverage-independent-review-9c7445e-20260926.md`
- `/var/tmp/ac-pr48-writers-branch-failure-probe.mjs` (`9bbfcb0d…`)
- `/var/tmp/ac-pr48-writers-switch-failure-probe.mjs` (`bb300571…`)

## Deliberately conservative object lifecycle

`patch-native-writer-failure-state.py` applies **after** the prior coverage patch.
A private JS field irreversibly invalidates the manager after any synchronous
mutator/transition exception, including validation failures and lock contention.
The original exception is preserved. Every subsequent mutation fails before its
body runs; neither `newSession`, `setSessionFile`, lower-level persistence/index
helpers nor an ordinary public property assignment can reset the private latch.

The exact pinned method inventory is exhaustive: 24 synchronous mutating/state
verification methods are guarded, along with 14 history/capability/witness read
methods. Basic diagnostic getters (paths, IDs, persisted flag) remain readable;
they are **not proof of usable session state**. `nativeInputProofAvailable()`
returns false on an unusable instance. Context/tree reads cannot silently supply
its partially changed memory for another operation.

This is poison-on-failure, not rollback. Disk evidence and partially changed
internal fields are not erased or restored speculatively. The failed object must
be discarded. Explicit recovery constructs a **new** manager from validated,
complete disk evidence; this does not resolve any UNKNOWN operation or authorize
resending it. Static factories work from disk, not a poisoned instance's cached
tree. Damaged files remain strict-load failures. There is no global file poison
or automatic repair, and no public reset/override for the object latch.

Successful operations keep their existing writer locks, revision checks and
return values. No timer/provider work or automatic retry was added. The burst
race fixture no longer retries even pure contention on a failed manager; one
aligned writer may commit a prefix and then retire on later contention. That is
an intentional availability tradeoff, not permission for other stale writers to
append. Runtime retirement/reopen integration remains required before activation.

## Exact artifact transition

| Item | Old reviewed NON-CLEAN artifact | Corrective candidate |
| --- | --- | --- |
| SessionManager SHA256 | `41a94b3777ac0ec322f649e3e234836893b8de86085f55a927ce29216205c28f` | `10ac30c15dd1b47b86fef4121c01d4b52b4a114cb9fcba6a909c04a3f5f88a7d` |
| Complete-tree SHA256 | `4a688172768342c219b48681587b3c8f20eb80db6732aa2fcea0fe5ed858c27d` | `7d16eb0199d7fc7ef119d75251b26be7eded8fd0672efdec96ba3a34299670ff` |
| Build suffix | `d45562f846a0afa3` | `1684f7d9f014feb8` |

Old canonical pointer `/var/tmp/pr48-canonical-package` is unchanged. New
canonical pointer `/var/tmp/pr48-state-canonical-package` selects a separately
prepared repository under `/var/tmp/pr48-state-prepare-yggLWz`. The new package
was rebuilt from stock through canonical preparation, not patched in place over
an installed or reviewed artifact. The new patch's exact `41a` → `10ac` transform
was independently rerun from an unchanged base and compared by digest.

## Continuation evidence

`test-native-writer-failure-state.mjs` exercises **13 failure scenarios × 38
continuations = 494 negative controls**. Each checks the required unusable error,
unchanged memory and unchanged filesystem after the failure. Origins include
both independent findings, direct internal switching, failed index rebuild,
branch summary contention, invalid new-session ID, stale revision, reconciliation
contention, partial branch/ordinary append writes, and branch/input/commit sync
failures. Public lookalike latch assignment cannot revive the object.

The two independent original counterexamples were rerun successfully against the
old canonical artifact. Both new negative scenarios first fail on that old
artifact with missing expected exceptions. New controls pass on the corrected
copy and on the fresh canonical build. Explicit fresh-manager recovery is tested
against the intact original source; after a commit fsync failure, a new manager
reconciles the exact ID as committed with exactly one entry and no resend.

The existing 19-case writer coverage, journal, writer exclusivity, hard-context,
manual/parallel compaction and input-recovery scripts also pass. The input-recovery
fixture now constructs a real in-memory manager rather than a prototype-only
mock lacking the new private state. Journal fixtures explicitly discard a manager
after exceptions instead of attempting to revive it by removing a lock.

Python MCP/provenance/authority/journal/ingress integration: **240 passed,
1 skipped**. An offline sdist → wheel build and extracted-wheel execution pass
**51 tests** using the wheel's actual module/resources (explicit asyncio auto
mode; the first config-free attempt omitted that test-harness setting and had
one async-collection failure). All eight native scripts and adaptive contracts
pass on the fresh canonical package; repeated preparation verifies unchanged
bytes and CLI version is `0.85.1`. Old canonical tree and installed manager
hashes remain unchanged. Logs: `/var/tmp/pr48-failure-state-*.log`. These results
are owner evidence, not independent clearance. Send the exact corrective commit and both
pin sets to `pr1-goal-p15-sink-independent-review` and
`pr1-native-goal-preflight-independent-review`.

Previous SQLite/watchdog narrow CLEAN and old complete-tree packaging narrow
CLEAN do not automatically cover corrected bytes. Node import closure (preserving
explicitly approved PR77 MCP extensions), adaptive runtime/idle-manager lifecycle,
ACP ingress, metadata publication/outbox and recovery/E2E still remain open.
Concurrent preparation's independently observed redundant outer stage remains
resource hygiene, not a demonstrated pinned-package corruption or bypass.
