# Summary admission carries its preparation

Source base: `6a787ae3959a949fea3f58885384ff50c51284c8` (merged559).
Changed production: `stack/native-compaction-selected-summary.mjs`, 5 lines
deleted / 6 added. No schema, protocol, settings, native pin or package changes.

## Owner and callers

`acAdmitSummary` prepares the cut once through native `prepareCompaction`.
Its existing admission carries the actual preparation and original binding to
`acExecuteSummary`, which passes that preparation to native `compact`.
`acSummaryCurrent` is source/route currentness, not another cut selection.
Deleted its second `prepareCompaction` call: every currentness consumer now
uses the same original cut. Also deleted the extra `acSummaryCompatible` call
at stream entry; currentness already checks that binding synchronously.

The five currentness consumers remain: admission after preparation, selected
provider entry, prefix construction after its await, completion, and known
provider-failure disposition. Source changes still fail through the original
`SessionManager.captureCompactionWitness`: persisted manager, loaded revision,
active branch, leaf, session identity/file and first-kept entry. Currentness
still checks queues, pending operations, idle/stream/retry state, selected
model/contextWindow, reserve/keepRecent settings, host session, runner/hooks,
stream function, provider/runtime/catalog and settings/session-manager identity.
Nothing grants a write, retry or UNKNOWN settlement.

`prepareCompaction` revisits context messages, the complete branch file ledger
and retained suffix selection. Those are prepared work, not independent
currentness evidence. Model contextWindow and compaction settings remain
explicit currentness inputs. Policy-derived selection belongs to that original
admission; a later policy is not permission to replace the admitted cut.
Native `compact` still checks actual generated/retained context through
`CompactionPolicy`/`ContextBudget` before returning. Its final budget checks
remain even if policy input changes; this change adds no policy snapshot.

Patterns: BOUND-1 (re-deciding already admitted work), BOUND-2 (bypassing the
actual prepared object). Existing source witness and final input budget are
different facts; both remain with their existing owners.

## Source evidence and verification limit

Before Python AST: 311 production modules, 13 stack Python modules, 53 tools,
361 tests, no parse omissions, through existing NRA `Package.load`.
Before JavaScript AST: 371 stack/native dependency modules, no parse omissions,
through Node's bundled Acorn. Calls by attribute name are candidates, not
proof of dynamic resolution; generated RPC injection is owned by
`patch-native-selected-compaction-summary.py` and was read separately.
After AST retains all five currentness calls, removes preparation from
currentness and leaves its single call in admission. The unchanged dependency
artifact is explicitly old code, not qualification of this new source.
Node syntax and `git diff --check` passed. No provider run or build occurred.

Next: Sch's reviewed artifact build, then one affected existing native boundary
batch covering original saved-source admission, changed source/settings refusal
and joined cancellation. Installed/native readiness is pending that artifact;
this is a source checkpoint, not Ready.

## Original555 timing remains separate

Original configured summary finished at 99.101s. Its first delta was3.258s;
the next distinct request prepared13.696s after summary finish. The original
native compaction entry timestamp is4.827s after finish, not a settlement clock.
Callback0.067ms and mostly<15ms native receipt delays do not explain that gap.
This deletion removes proven repeated preparation; its duration contribution
has not been measured. No original source, journal, proof or input was changed.
