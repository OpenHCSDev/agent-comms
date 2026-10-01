# Public terminal retirement capability

Arendt owns this contribution to Toad274. Parent granted only the original
child_process retirement join declaration and every original caller. Current
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

Toad274 actual paired ACP/PTY receiving acceptance remains pending. No second
native fixture or build is requested for this algorithm-preserving contribution.
