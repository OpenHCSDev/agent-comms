# Native launch settings: implementation checkpoint

Owner: core-goal-retry. GoalDetails (#958/#689) remains paused.

## Source findings

At base `50f2b0e0`, production, tools and tests contain 26 binary-variable and
22 argv-variable references. The NRA source pass covered 1,072 modules across
Core production/tests/tools, paired Toad and the installed metaclass-registry
root, with no parse omissions. The census and contexts are retained under
`/home/ts/.cache/agent-scratch/core-goal-retry/native-thread-settings/`.
Syntax coverage does not resolve arbitrary dynamic callers or embedded programs.

`NativeArguments.configured_binary()` preferred a truthy parameter, then an
ambient environment value, then a string default. `configured()` independently
read and shell-decoded the ambient argv value. OwnerLifecycle wrote these facts
into child environments; RetainedOwnerLaunch read them independently at restart.
Worker.run instead selected the launcher beside its Python executable.

`NativeThreadDefaults` already declares native defaults. It now owns binary and
argv together. The installation build record declares version/package identity,
not per-launch argv. Private launch selection still resolves the reviewed native
entrypoint before constructing the immutable settings value.

## Handoff and lifetime

NativeArguments requires the typed owner; it no longer reads an environment,
accepts an environment mapping, or chooses a fallback. Existing explicit API
parameters are bound once through NativeThreadDefaults.for_launch.

The existing ParentedProcess launcher gains descriptor forwarding, using its
existing Platform.launch mechanism. NativeThreadDefaults lends a sealed anonymous
memory descriptor, encoded by FieldCodec. Only its descriptor number appears in
the worker command. Arbitrary native argument values must not become newly
visible in the worker's public command line. Parent closes its handle after
launch; the owner keeps its inherited handle for its lifetime. No persistent
configuration file, second store or environment override is introduced.

RetainedOwnerLaunch reads that original process's descriptor and retains the
typed value along with its existing original-process/environment observation.
The existing identity checks still bracket capture. Queued restart target
selection takes the typed value, not a mapping of ambient strings.

The anonymous sealed handoff currently requires Linux memfd support. This is an
explicit implementation constraint, not claimed portable native-worker support.
Old workers without a typed descriptor refuse capture. There is no legacy env
reader. A live rollout therefore needs reviewed old-source capture before stop,
then explicit typed launch into the new runtime; do not switch a live fleet and
assume old restart capture remains supported. Original sessions and uncertain
inputs must remain untouched.

## Unfinished / not acceptance

This is an implementation checkpoint, not a working delivered build. Remaining
consumer migration includes ACP/installed journeys, fixtures, removal of obsolete
forwarding properties and focused checks. The original 48-reference census has
not yet reached zero. No tests, native start, shared installation or UI acceptance
have been run for this change.
