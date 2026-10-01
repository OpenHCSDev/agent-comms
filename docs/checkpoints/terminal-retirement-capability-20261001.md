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
Every original Core start/session caller continues to use streaming defaults; none
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

The receiving Toad admitted_spawn/AgentProcess family also consumes the original
ChildStdio member directly; the ACP10MiB stream budget is unchanged. Removed
scalar kwargs have no compatibility path. This crossing was caught before staging.

## Scoped installed receiving acceptance

One actual installed ACP/native/PTY journey passed in22.609s with Coref8ae4f52 /
Toad4552528d / Textual6b / SDK0.12.1 / native593. All69 packages, Git source/assets
and native full trust were verified. The normal ACP launch consumed the new
stdio family; no source overlays, dependency bypass or native rebuild.

Canonical receipt in the paired Toad274 branch:
`evidence/terminal-execution/installed-custody-20261001.json`, SHA
`a326895c340773086de905a33d093ee5396ca234d7e972357213d5f5b71cae21`.
Persistent receiving path:
`/home/ts/wt/toad-terminal-execution-custody-20261001/evidence/terminal-execution/installed-custody-20261001.json`.

Normal exit, ACP SIGKILL, cancelled wait, two delivered release cancellations,
missing cwd, bounded UTF8 and exited-leader descendant retirement passed through
the original owner. Actual detach/tab-click return retained the same ANSI/resource;
Arendt personally viewed both restored output bodies. Original PTY masters[]→[]
and addresses0; both private native owners exited with groups empty after shutdown.
Zero provider POST/paid/public/default changes or input replay.

Startup cancellation proved pre-acquisition refusal only; acquired-startup
cancellation was not exercised in this installed run. This is scoped paired
terminal acceptance, not public activation, X11/CPU or all comms workflows.
Production remains10lines deleted/42added; receiving source and fixture counts
are separate. Documentation/evidence after f8ae change no tested production code.
CI deferred; parent owns merge/publication. Ready at this measured strength.
