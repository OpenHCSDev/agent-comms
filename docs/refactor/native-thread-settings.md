# Explicit native launch settings — unfinished replacement

Owner: core-goal-retry. Draft #984 supersedes #978 without rewriting its
published checkpoints. Neither is merged or the default installation.

## Ownership and deletion

`NativeThreadDefaults` owns binary, tuple argv, model and thinking defaults.
The only process transport is `--native-settings JSON`, encoded and decoded by
FieldCodec. The FD flag, sealed payload machinery, temporary payload files,
Linux constants, added pass_fds parameter and constructor type rechecks are
deleted. No environment reader or legacy fallback replaces them.

Parser construction supplies its typed default explicitly. Namespace lookup
has no hasattr/default fallback. Comms consumes a decoded CommsRoute and required
settings; for_runtime, bound and wire forwarding factories are deleted. The
route owns binding, with PinnedRoute sharing the environment/publication facts.
Read-only recovery/response consumers use their actual bus, release and Goals
owners instead of constructing launch defaults. SelectedExecution receives its
caller's Comms rather than reconstructing its configuration from a path.

ACP receives a required NativeThreadDefaults instead of optional binary/argv.
Its callers bind an explicit record; no optional scalar compatibility reader
remains in CommsAgent. The argument parser's two forwarding configuration
methods are deleted; consumers read the record and use NativeArguments.parse.

## Source evidence and checks

The initial census had 48 references to the two removed environment keys;
production/tests/tools now have zero. NRA's pre-change inventories include
production, tests, tools, paired Toad and the actual metaclass registry:
1,072 parsed modules, no omissions. One-use byte editors perform the recorded
constructor migrations; they are not behavioral proof or another audit tool.
Embedded programs and the paired consumer still require final reconciliation.

The superseded FD checkpoint started an installed worker and loaded it over
actual official ACP, with zero inputs and exact launch values; its processes
were retired. That result is retained but does not accept the replacement.
A real source CLI invocation with explicit JSON settings returned the normal
tool catalog on an isolated root after the FD deletion.

The earlier focused batch had 51 passes and two fixture failures: an obsolete
SystemExit expectation (the parser raises ValueError) and a direct Unix connect
that bypasses PrivateSocketRole.address for a long root. Final focused checks,
ratchet, private wheel and actual JSON-channel native start are unfinished.

## Runtime dependency

Codex owns LaunchRuntime in owner_launch.py on fix/current-runtime-launch,
checked API a4d54453. It selects current once, owns interpreter/native entrypoint
and constructs the target environment. Integration must remove the remaining
sys.executable-based native default getter; no second runtime resolver is
permitted. This checkpoint is not ready for review/installation until that
integration and the remaining consumer reconciliation are complete.

#958/#689 GoalDetails remains paused. No uncertain input was replayed; preserved
sessions, failed roots and original evidence are unchanged.
