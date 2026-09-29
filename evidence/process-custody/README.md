# S13 process custody closure

**125 production lines deleted,159 added.** No file relocation or compatibility entrypoints.

## Ownership and deletion

Boyle owns `child_process.py` process custody and canonical lifecycle/test callers.
Claim posted on PR383 after bounded review of Wegener19bdffd9 (READY). Parent owns merge/install.
S13/A12 current closure extends existing custody: no new process supervisor, registry, or native proof format.

- Delete `DetachedProcess` optional-Popen representation, all `attach` factories and callers.
- `ParentedProcess` owns OS child handle, reap result and pipe retirement. `ObservedProcess` owns recorded-incarnation observation/signalling and cannot claim a parent's exit code. Shared stop algorithm remains `ChildProcess`.
- `ChildLaunch.spawn` is the single synchronous gated creation. Deadline and normal launches consume it.
- `InheritedDeadline` owns watchdog/pidfd acquisition and reverse-order retirement; delete nullable child/watchdog/pidfd cleanup roster and foreign stream iteration. Child retires before watchdog; inherited authority descriptions are never unlocked by cleanup.
- All production callers, tests, executable evidence probes migrated; no aliases or old API.

Latest global AGENTS, NRA, resolved exact refactor-audit and S13 reread. Patterns: IDEN-3 optional custody state, IDEN-8 exact process identity, IMPL-13 one process mechanism, TIME-3 deletion without compatibility, AGENT-6 ownership rather than relocation.

## Actual evidence and readiness

`actual-child.txt`: 22 PASS24.06s, actual OS children/process groups/pidfd/inherited locks, repeated cancellation, mismatched identity refusal, owner-only reap result and exception pipe cleanup.

Noneditable package paths and complete immutable native verification are in `installed-imports.txt`; package is `/home/ts/.local/share/agent-comms/native-current-9213ee71479d1b20/node_modules/@earendil-works/pi-coding-agent`. Source51456661 includes current main a2dee9c4. Subsequent commits change only tests/receipts.

- `installed-native-owner.txt`:12 PASS39.39s. Actual native commit, unsettled UNKNOWN refusal, lost-result reconciliation without replay, parent SIGKILL before native write retaining authority, seven real fenced retirement/restart cases. At this first pass the seven retirement fixtures still supplied source PYTHONPATH; the final eight-case pass below deletes that override and tests installed children.
- `installed-acp.txt`:1 PASS26.66s. Actual saved-history ACP startup, native automatic channel reply, bounded consideration, settled owner, guarded restart; loopback provider only, no paid calls.
- `installed-callers.txt`:26 PASS,1 platform skip,2 fixture setup failures from omitted `PI_COMPACTION_TEST_PACKAGE`. Kept original receipt.
- `installed-owner-callers-final.txt`:correct package gives real installed owner start/restart/stop PASS; retained RED exposes preexisting removed `ownerPid` field in the client-loss test. Migrated this test's owner PID and settlement assertions to canonical `CoordinationChangedUpdate`/`TurnSettledUpdate`, preserving exact owner and no-duplicate assertions; removed both source-PYTHONPATH overrides.
- `installed-custody-final.txt`:8 PASS20.69s. Real native owner survives ACP client loss; two clients reattach to the same incarnation; provider called exactly once; actual typed settlement observed. Seven release/escalation/restart fences pass with installed subprocess code.
- `ratchet.txt`:all changed production measures unchanged, no added chain terms, foreign absence probes, codec subclass, or god-class excess. `lint-final.txt`:focused lint clean. Deleted API/private-parent-field search finds zero executable callers.

**READY PR386.** No remaining diagnosed process-custody blocker. Linux actual evidence; no claim of native macOS/Windows execution. No live changes or native package edits. Parent owns merge and live installation; CI deferred.

Owned disposable `.venv`, `.installed` and `.scratch` are removed after completed test processes exit; cleanup receipt records the paths. Source/receipts retained.
