# Ready: retained facts borrow the original source

Production checkpoint 0ff77a6a: four existing files, 35 lines added /44 deleted.
No native, schema, declaration membership or wire format changes. No new class,
cache, store, counter or authority. Draft #578 reuses the original checkout.

## Changed ownership

`CertifiedSourceRead.retained_task_facts` captures complete original
sender/addressed membership through `conversation_sources`. The existing
`CommittedDelivery.compaction_messages_for` owns which declarations apply;
`Message`/task classes own the actual facts. SQLite's negative LIMIT means this
complete relation is not truncated to a UI page. Original sequence order and
duplicate fact occurrences are preserved. Public retained records and silent
observations contributed no compaction messages before and still do not.

`HeldCompaction` borrows the certificate from its actual acquired StoreLock.
Capture/currentness reads only this owner's original pointers, without opening
another BUS lock or scanning every unrelated record. Native/registry/input/
settings and retained-source equality fences remain. Wire and registry/input
custody remain held through native commit; the original inherited FD grant is
unchanged.

`WireLog.retained_sources` shares that reader for inspection, export and selected
route context. It captures bytes inside wire -> bus -> registry/input custody,
then decodes outside publication locks. The returned observation is not a live
admission permit. Delete `_retained_task_facts`,
`retained_task_facts_unlocked` and the unused base WireRecord decision.

AST before/after: 726 Python production/test/tool modules, no omissions. The
after census includes the other native/input/message fact declarations, which
own different evidence. Attribute resolution remains a stated ambiguity;
semantic reads cover the actual resource, query and applicability consumers.

## Installed verification

Normal wheel+[acp] resolution in released existing 534 holder; all 342 shipped
members equal the installed files and every Python member equals source.
Previous accepted 576 wheel/source proof and all original forks remain preserved.
No new environment/native copy. Resource check: home4.5GiB, RAM19.5GiB;
the reused holder and serial checks add only a small wheel/test output.

Five installed controls passed in 8.69s. They detect:

- Display-page truncation (>100 original sources), foreign-source decoding,
  decoding under publication custody, and importing a later append into the cut.
- Loss of original user/goal/failed-input facts or missing source-change refusal.
- Cross-audience original/correction leakage and altered owned lineage.
- Changed inspection/diff/export ownership, protected bytes or atomic export.

Actual installed CLI on the preserved 576 real SDK-fork/native/ACP root:
`retained-context openhcs-helper2` succeeded in0.822s and `compaction-status`
in0.655s. The saved native history is43,205,875 bytes; 21 protected original
files retain exact hashes. This root's ordinary channel exchange contributes
**zero retained task declarations**, agreeing with its original verified scan;
the declared fact/compaction currentness controls above cover nonempty cases.
This is an actual read-only installed path, not a new native input, provider,
summary or physical-UI acceptance. No public mutation or replay.

## Remaining latency

This deletes full-bus decoding from compaction capture/currentness and retained
inspection/export. It does not establish the cost of that work in the original
13.562/13.696s next-request gap. The child -> external commit helper -> fresh
child split remains: retirement currently prevents stale in-memory SDK state,
and the helper still opens native history while holding global commit custody.
The original98.141s selected-provider duration is a separate span, unchanged.
Continue that source/lifetime family without repeating an unchanged provider run.
