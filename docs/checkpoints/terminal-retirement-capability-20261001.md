# Public terminal retirement capability

Arendt owns this contribution to Toad274. Parent granted the original
child_process retirement join declaration and every original caller, then the
existing AttachedChild/Platform launch-IO capability after finding the concrete
exited-leader resource leak. Initial
base is 464b05dc. No native/provider/runtime format changes or public effects.

The existing cancellation-resistant join must be available to the real PTY
acquisition and release scopes. Rename `_join_retirement` to `join_retirement`
and migrate both original Core callers. Delete the private name; keep the
algorithm identical. No wrapper, alias, copied loop or separate custody store.

IMPL-13/TIME-2: one resource retirement algorithm owns repeated cancellation.
Toad274 consumes it at acquisition and release, joining the original task before
propagating caller cancellation. The receiving acceptance is the existing actual
ACP/PTY journey, including cancelled release and startup resource cleanup.

Source controls and installed receiving acceptance will be recorded separately.
Frozen public479 and its already reviewed pair remain unchanged.

## Source checkpoint

Production: 3 lines deleted, 3 added, solely the declaration and its two original
call sites. AST comparison confirms the join algorithm is identical after the
declaration rename. No private name remains in source or tests.

Existing real-process cancellation controls: 3 passed in 7.81s, 16 deselected.
Command (source control, not installed receiving acceptance):

```
PYTHONPATH=src /home/ts/wt/comms-native-admission-epoch-20261001/.observations/installed/bin/python -m pytest -o addopts='' -q tests/test_child_process.py -k 'cancellation_retains_child_cleanup or repeated_cancellation_joins_real_tree_before_return'
```

## Exited leader and original group custody

Parent review found that the initial Toad PtyProcess checked the shell return
code before signalling a bare process group. An exited shell can leave a child
holding the PTY. Toad now acquires the original AttachedChild through its existing
platform exec gate and identity-bound group owner, deleting its manual process
spawn, bare group signal and independent retirement timeout.

The ChildStdio family declares the launch IO boundary. StreamingChildStdio owns
the unchanged PIPE/DEVNULL and 65536-byte streaming defaults. TerminalChildStdio
owns the original supplied slave file. AttachedChild.start consumes those
declarations through the same existing launch/identity/verify/retirement path.
Every original start/session caller continues to use streaming defaults; none
supplied the removed scalar input_enabled/limit parameters. No caller maintains
a parallel stdio roster or starts a process outside the original owner.

ChildProcess.force signals its original group through Platform. The existing
SynchronousProcess force authority guard delegates to that one owner; AttachedChild
can retire surviving members after its leader exits. stop/stop_plan and the
cancellation-resistant join algorithm are unchanged. No local signal/stop loop.

Current production versus 464b05dc: 10 lines deleted, 42 added in the original
child_process owner. All 22 existing child-process and guard controls passed in
25.68s. Actual source PTY control with `(trap '' HUP TERM; sleep 30) & exit 0`
observed one surviving original member after leader exit; release retired it and
closed the PTY in 2.075s. These are source controls, not installed/live claims.

Toad274 actual paired ACP/PTY receiving acceptance remains pending. Its original
journey includes this new descendant control. No second native fixture or build
is requested. Production and fixture deletion counts are recorded separately
in the receiving Toad checkpoint.
