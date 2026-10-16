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
LaunchRuntime, then derives its native defaults; for_runtime, bound and wire
forwarding factories are deleted. The
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

## Integrated source checkpoint, not installation acceptance

The fresh branch includes Codex's checked 0532d3b3 runtime implementation.
LaunchRuntime/RestartEnvironment live in launch_runtime.py, without re-exports.
NativeThreadDefaults.from_runtime(runtime) consumes the selected value; the
sys.executable-based getter is deleted. Workers use running(), not current(),
and receive a recorded ThreadIncarnation through its declared JSON flag.
The identity API is based on #965's family at 2979497b plus b38b608c. Later #965
heads remain to be integrated after review; no competing identity definition
was retained. The API checkout /home/ts/wt/core-thread-launch-api is retained
for the active shepherd/#965 consumers.

RestartTarget names selection and complete settings. NativeChoice owns
KeepRecorded/Replace behavior; normal restart resolves each original's exact
settings, while explicit flags construct Replace at the CLI boundary. Start
and creation require complete native values. CliOption extends dataclasses.Field
and owns its former parser projection; CliRecord composes nested field updates
through the same option declarations. The overrides dictionary, apply_cli
wrappers, anonymous target pairs and scalar nullable launch overrides are deleted.
RegistrySnapshot owns live restart selection instead of six empty requests.

NRA's CLI/restart inventories parse 1,388 modules with no omissions. A source
application parser check passes KeepRecorded, explicit binary/argv Replace,
Start's typed default and empty-binary refusal. The source catalog invocation
reached the normal CLI error boundary but refused an uninitialized private bus
(no durable marker); its isolated root is preserved. No worker input was sent.
The old installed FD run does not verify this checkpoint.

Remaining: reconcile the eight explicit-record installed/native fixtures and
new required start/create signatures, embedded programs, paired Toad, the latest
#965 family and shepherd's login factory; then final focused checks, ratchet,
private wheel and actual JSON worker/ACP start. Do not run the unreconciled
native fixtures: losing their explicit offline/context flags is unsafe.
The migration helper write returned tool admission UNKNOWN; its destination
was subsequently absent. That uncertain write was not retried. A separate
source edit migrated 234 files/820 constructor sites. These are source edits,
not behavioral proof or a default installation.

#958/#689 GoalDetails remains paused. No uncertain input was replayed; preserved
sessions, failed roots and original evidence are unchanged.
