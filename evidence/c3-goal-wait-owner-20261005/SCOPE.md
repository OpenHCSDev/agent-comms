# Dependency wait current-owner decision

GoalWait.current_for owns applicability to an owner incarnation and delegates
identity/revision to the existing Goal.accepts_observation. Four copied policies
are deleted: wait-graph node, wait-graph peer, closed-wait recovery and terminal
turn recovery. Optional lookup, process/turn custody, status, locks, reply reads,
write ordering and clear semantics remain with their existing owners. Reply
consumption intentionally retains its incarnation-only check.

The four callers already select waits by goal ID. The delegated check also
refuses a malformed lookup containing a row for a different goal. Later goal
revisions retain waits; future wait revisions and replacement owners refuse.
No stored fields, codecs, runtime schema, migration, native operation or input
lifecycle changes.

## Verification

Existing Repository/Package source read: 324 production,369 test,54 tool modules,
zero omissions. Before source sites are recorded in source-before.json.
Five authored offline wait-graph cases PASS in1.97s using host Python3.14 and
original Comms/Registry/GoalWaits on new private authored stores. No native,
ACP process, SDK, provider, original session, public root or installed prefix
was used. Test scratch is removed after terminal.

The first existing-test command stopped before collection: host lacks the
configured xdist plugin. Serial existing standby modules then stopped during
collection with ModuleNotFoundError: acp. No dependency installation or borrowed
holder followed. The first authored graph control had five assertion failures:
it expected terminal leaf nodes in the group instead of only waiting owners.
Its exact source is preserved in control-before-graph-result-correction.py;
only expected group membership changed for the final passing run. Production
was not altered in response to those authored expectation failures.

This is source qualification only. Existing installed ACP terminal/recovery
checks still need a fresh matching installed-source purpose; no live-readiness,
performance or complete six-file C3 claim is made.

After migration:324 production,370 test,54 tool modules parse with no omissions; source-after.json records one GoalWait declaration and four migrated calls, with zero outside wait-revision comparisons. An initial source selector assertion counted unrelated current_for owners; it was corrected to the actual GoalWait declaration and source-read receivers, without changing production or repeating tests.

## First installed attempt and certified fixture correction

The one issued installed attempt exited1 in6.110651239s: six failed and three
passed in3.12s. Every failed fresh standby graph case stopped at the original
WireLog private-marker requirement, before the graph assertion. Parameterized
cases explain nine executions from seven selected nodes. Original controller
1410583/birth65096806 and child1410603/birth65096903 retired with no group members.
The original5f package was restored once; all510 recorded original files and
347 ZIP assets matched, with no added candidate assets. No native/ACP process,
SDK/provider input or public runtime operation occurred. Whole claims returned.

The changed source control now uses the existing canonical_goal_wire for all
ten fresh Comms constructions. The one crash-recovery reopen still uses wire
and all40 assertion ASTs are unchanged. It initializes only new authored empty
buses through the existing Messaging issuer; no reader guard changes. The
installed failure and its old control stay immutable. This correction has only
source/AST verification so far: the host has no ACP dependency, and the expired
installed purpose will not be borrowed or repeated. A changed-control installed
check needs a fresh specific purpose after independent closure. No new wheel
is needed because production/build inputs are unchanged.

## Changed certified controls installed acceptance

Fresh distinct purpose f05a169 authorized the corrected original seven-node
command once, after exact355 candidate ZIP/installed byte/mode proof1cfae0b0
and unchanged94 unique nonCore keeper paths (154 overlapping authority records).
All nine parameterized executions passed in5.00s, bounded terminal0/8.242110359s.
This accepts installed original Comms/registry/wait graph and terminal-fence
recovery. It does not prove the live native done callback or whole C3.
Controller1538133/birth65148416 and child1538348/birth65148515 joined, retired
and were absent; groups empty. The actual normal original5f restore ran once.
All510 original recorded hashes/modes/links and347 original ZIP assets matched;
no added candidate assets. Authored scratch removed. Whole handback fcd8128b
binds14 raw/proof/restore keeper hashes and explicitly returns all package,
import/read and execution claims. Earlier ce6 failure remains immutable.
No native artifact/App/ACP process, SDK/provider input or public/session/root
operation. Independent Bohr final floor/census closure is pending separately;
source product/build inputs stay equal the original8546 wheel, no rebuild.

Independent final closure now verified: lifecycle1e0759f2 and floorreadback
77c955eb, fresh235 UID holder/output references/gaps zero. All14 rawkeeper hashes,
original510 mode/hash/link records,347 ZIP assets/full10 versions/origin match.
No remaining private package/import/read/execution claim. Publication into the
user's default runtime remains a later explicit current-package operation;
the frozen Explorer candidate is not changed by this source merge.
