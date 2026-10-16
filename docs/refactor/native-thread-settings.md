# Native binary and argv: explicit typed settings

Implementation owner: core-goal-retry. GoalDetails (#958/#689) stays paused.
The current-main branch supersedes draft #978 without rewriting its published
checkpoint history. Neither branch is installed or accepted.

## Source and ownership

Starting main `50f2b0e0` contained 26 binary-variable and 22 argv-variable
references across src/tools/tests. Both names now have zero references, as does
`NativeThreadDefaults()`; there are no newly added Optional or bare-settings
lines against that starting main. Checks and NRA evidence are retained under
`/home/ts/.cache/agent-scratch/core-goal-retry/native-thread-settings/`.

The NRA pass covered 1,072 modules across Core production/tests/tools, paired
Toad and the actual metaclass-registry dependency, with no parse omissions.
Syntax does not resolve arbitrary dynamic aliases or embedded foreign programs.
The string-based launcher mock was read and migrated explicitly.

The old NativeArguments getters independently selected environment strings and
silently replaced a false binary with another source/default. OwnerLifecycle
wrote those strings; retained restart capture shell-decoded them independently.
Worker.run instead selected the launcher beside its interpreter.

NativeThreadDefaults now owns the required binary and immutable argv tuple.
NativeArguments.configured/configured_binary require this value. They have no
environment parameter, environment lookup, override key or default parameter.
The physical runtime binary is derived in exactly one production method:
`NativeThreadDefaults.runtime_binary`; both former launcher methods are deleted.
The installation build record identifies package versions, not per-launch argv.

Settings parameters are required. Application entrypoints load the value from
the running installation or decode the explicit CLI/worker value once and pass
it down. Existing explicit SDK scalar overrides are decoded at that boundary,
not inside the NativeArguments getters. Empty binaries are rejected, including
restart overrides before any original is fenced or stopped.

Comms requires the settings at composition. Its named `for_runtime` factory
binds ordinary entrypoints to their actual installation; `bound` owns the route
binding formerly duplicated at the adapter boundary. A byte-preserving edit
migrated 307 statically imported constructor calls in 135 files. There were no
shadowed callees or forwarded keyword dictionaries in that set. Existing wire
callers retain the existing wire entrypoint, which loads from the typed runtime
owner and delegates route binding. No settings object is a parameter default.

## Handoff and lifetime

The existing ParentedProcess forwards descriptors through its existing
Platform.launch mechanism. NativeThreadDefaults lends a sealed anonymous memory
descriptor encoded by FieldCodec. Only the descriptor number is in the managed
worker command; arbitrary argument values must not become newly public argv.
Parent closes its handle after launch. The owner keeps its inherited handle,
marked close-on-exec after decoding, for its lifetime. Platform owns creation,
validation and reading: Linux uses libc memfd plus kernel seals; POSIX closes all
writers and unlinks a private read-only payload before passing its descriptor.
POSIX temporary resources belong to Platform under
`~/.cache/agent-scratch/agent-comms-launch/payload-*` and its context removes each
run directory. Linux uses no filesystem payload. There is no persistent
configuration file, second store, global configuration cache or env override.

The public CLI accepts an explicitly supplied settings value through the same
FieldCodec; this public argument form is not for credentials. When omitted, the
entrypoint binds the running installation, not a hidden settings parameter.

RetainedOwnerLaunch captures the original process's sealed descriptor under its
existing exact-process checks. Consumers take `launch.native`; its old forwarding
binary/arguments properties are removed. Queued restart target selection also
takes the typed value, not an environment mapping. Both worker entrypoints now
share parsing, accepting their own exact pre-registered process identity while
refusing another live owner.

Retained `/proc` capture remains Linux-specific, as before this change. The
private descriptor transport preserves the existing POSIX platform family. Old
env-only workers refuse capture; there is no legacy reader or guessed value. The existing explicit
old-source projection/stopped carry therefore needs review before default fleet
installation. Change-report owns that independent producer-boundary adjustment
or a precise required retirement; core-goal-retry retains this implementation
and fresh installed-worker verification. Original histories and uncertain inputs
remain untouched.

## Confirmation status

The real source CLI decoded an explicit typed binary/argv selection and returned
the normal tool catalog on a new isolated root. It did not execute that binary,
attach to the live bus, submit input or contact a provider.

The three existing Core/Toad fixture command strings use one ordinary imported
`native_run_command` helper. No automated Toad tests or synthetic applications
have been run. The CLI parser snapshot also had existing baseline drift: the
installed baseline already added `new` and changed fork/target command help.
The snapshot now reflects those accepted declarations plus the new global flags;
none of the candidate subcommand help differs from the installed baseline.

Current main through `fe731cd23` is integrated by rebasing this new branch; its
claim-carry deletion and current report-producer fixes are preserved. The follow-up
static constructor census has zero missing typed compositions. The exact requested
`git diff origin/main -- src tools | rg '^\\+.*(\\| None|NativeThreadDefaults\\(\\))'`
check has no output.

The private wheel was built and installed with unchanged dependencies from the
then-current physical runtime. Imports and the public typed codec roundtrip work.
The first actual CLI worker start failed before ParentedProcess.launch: portable
Python omits os.memfd_create, its flags and fcntl seal constants. Its fresh root
`/home/ts/wt/native-settings-start-c6b801c3` and receipt are preserved, with zero
submitted inputs and no live owner. Platform now owns that OS operation, using
Linux libc/kernel ABI declarations rather than assuming optional Python exports.
A new isolated phase will confirm the repair; the failed root is not replayed.

Focused checks, receiving and successful fresh native worker start remain
unfinished. No merge, default installation,
provider-answer or physical UI acceptance is claimed.
