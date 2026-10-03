# 558 Ready: idle source acquisition

Production source: cc64bf6bc7e6f30e513f4fea18a50cb580efe8aa.
Two production files: **18 lines deleted, 62 added**. Native, schema, delivery,
source-coverage and required prompt-admission code are unchanged.

## What changed

554's opportunistic observation requested a nonblocking bus EX lock and replaced
Coordination's existing read acquisition policy with zero wait. Ordinary physical
contention therefore escaped to the existing unavailable-drain health path.
The original new activity errors establish fresh idle failures, but contain no
traceback naming the first failed syscall. This change removes those observation
choices; it does not claim a historical persistent lock leak or full latency fix.

WireLog now owns one certificate/marker/currentness body for synchronous and
asynchronous consumers. StoreLockContention still owns the physical acquisition
algorithm. Acquisition waits asynchronously; the existing joined Coordination
worker certifies and consumes the source on its own thread. Its callback returns
only the detached original wire cut. Source/SQLite resources and the bus lock end
before the registry/coordination observation begins. Cancellation before acquire
closes this waiter alone; after acquire, cancellation joins the owned work before
closing its descriptor. No SQLite connection crosses to the event loop.

InputDrain retains the exact pre/post wire, owner, participant, pending, cursor,
configuration and source-inode comparison. Required delivery/admission remains
nonblocking where it was nonblocking. No generic catch, extra retry, queue, cache,
semantic state, timeout or schema was added.

## Exact currentness lifetime

The provisional finally-currentness check was removed before qualification.
The final shared body checks the original source after **successful** consumer
return, before certificate and physical custody end. That is the original sync
contract, now shared by sync and async callers. It certifies the returned detached
cut and refuses replacement or in-place changes before release. If the consumer
raises or cancellation joins a failed worker, resource teardown still runs;
a second currentness refusal must not replace that original error. All existing
sync certified_read consumers use the same body; their contract is unchanged.

## Source and consumers

Existing NRA Package.load parsed all 724 declared production/test/tool modules,
with no parse omissions. Before/after declarations and candidate imports,
references and callbacks are attached. The after inventory also names the four
new methods; candidate counts therefore are not a debt delta. Dynamic attribute
resolution remains a semantic-reading limitation, not zero by omission.
InputDrain's sole observation caller and its original direct test caller migrate
in this batch. Existing synchronous certified readers remain on their original
entrypoint and now consume the shared lifetime. StoreLockContention,
_held_store_source and Coordination.run_worker are the existing owning mechanisms.
Patterns: IMPL-12 (one lifetime body instead of copied sync/async decisions),
BOUND-2 (consume the original certified source, not a guessed bus revision).

## Final changed-boundary checks

One batch, source interpreter Python3.14:

    PYTHONPATH=src .artifacts/runtime-scoped-input534/bin/python -m pytest -o addopts='' tests/test_private_idle_drain.py::test_certified_observation_waits_and_joins_its_owned_worker tests/test_private_idle_drain.py::test_new_inputs_and_recovery_revision_invalidate_idle_observation tests/test_private_idle_drain.py::test_during_scan_change_is_not_absorbed_as_idle -q

Actual terminal output: `3 passed in 2.24s`.
The first check holds the real original private bus resource, verifies pending
observations and a responsive event loop, cancels one waiter without disturbing
others, releases into equal observations, and verifies acquired-worker cancellation
retains physical custody until callback completion. Original callback refusal is
preserved and resources release. The other two checks prevent absorbing changed
original pending/recovery/wire facts during or after suspended observation.
No native child or provider is used; original disposable fixture roots retire.

## Actual installed original scope

Normal wheel build/install reused the released 69-package holder. pip check passed,
SDK0.12.1 remained unchanged. No source PYTHONPATH or new environment.
`installed-readonly-owners.py` exercised the changed installed observation method
against the actual default public root and its **19 alive, idle original owners**.
All 339 installed source files equal the published candidate. The concurrent reads
completed in **0.656674 seconds**, without exceptions. All original thread records,
process identities, admissions, source revisions, input-proof hashes, bus revision
and marker hash stayed unchanged. No queue drain, configuration refresh, cursor
advance, input, provider call, runtime client, native child or public owner change.
Original bindings are local controller resources only; runtime is disabled.

Exact inputs/results are in installed-receipt.json, installed-source-proof.json,
installed-readonly.log and install.log. This qualifies the installed read boundary
and original idle roster; it is **not** patched public workers, a physical UI
readback, or a claim that terminal/channel latency is solved. Parent owns normal
installation and the actual default UI readback. 555 native work is independent.

## Resources

Reused holder:
/home/ts/wt/toad-receiving-485-488-278-20261001/.artifacts/runtime-bundled-485-488-278-20261001

Owned disposable wheel:
/home/ts/.cache/agent-scratch/mendel-idle-source558-20261003/wheels

No public default, native960 or protected donor/source/history/UNKNOWN was changed.
No owned subprocess remains. Installed holder remains a candidate borrower until
receiving installation releases it; preserve original evidence and native donors.
