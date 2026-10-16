# Native runtime and settings ownership (P18, P40, P41)

Owner: core-native-runtime. Draft #1007 is unfinished; source checkpoints are
not installation or provider acceptance. P55 owns the forms and ACP/Core split.
Shepherd owns the separately published caller/acquisition/shutdown changes.

## Facts and consumers

| Fact | Owner | Consumers and removed choices |
| --- | --- | --- |
| Selected installation | `Comms.runtime`, implemented by `LaunchRuntime` | Worker launch, native launch, metadata/configuration, fork transport. No settings binary, `sys.executable`, executable-name allowlist or source-stack launcher fallback. |
| Native arguments and creation defaults | `Comms.settings: NativeThreadDefaults` | Thread management, configuration catalogs, worker construction and CLI. Removed copied native records from CommsAgent, TurnRunner and SessionLifecycle. |
| Configured native SDK artifact | OwnerLifecycle's existing explicit launch selection | Metadata/native factories and the fork request receive its package explicitly, not another ambient-route lookup. Copying verifies the artifact inside the selected installation. |
| Python import isolation | `LaunchRuntime.command` | Worker, runtime helper, exec gate and internal child commands use its command. RestoredRuntime preserves its deliberately original import environment. |
| Fork operation | PiNativeBackend and the existing PiHelper machinery | RuntimeHelper composes the transport with ForkSessionHelper through C3 MRO. The selected installation imports the copying module and runs its SDK. The caller does not run its own copying implementation first. |
| FULL turn entry | `SelectedExecution.full` | Both direct FULL and TRIAGE→FULL enter it. It alone calls the action/tool selector. The execution owns its attempt limit. |
| Required integration | NativeRequestTools and NativeProject declarations | NativePiRpcLaunch loads their immutable entries even with discovery disabled. Tool restriction remains the selected mode's existing native arguments. |
| Extension identity | Native import-fence declarations | Source aliases and packaged entries resolve to one entry. The SDK loader uses its existing loaded-extension collection to avoid initializing one extension twice. No second startup catalog subprocess or new cache. |

A worker identifies the installation already selected by its parent using
`LaunchRuntime.running()`. It must not follow a changed current pointer. Parent
exec gates/watchers identify their own installation for a different reason;
those identities are not merged with the selected native child's identity.
`running()` identifies `sys.prefix`, not `sys.executable`.

Settings JSON belongs to the existing JsonFlagRecord/FieldCodec implementation.
The CLI constructs Comms with its settings at the boundary; Start does not
replace them. Restart's explicit KeepRecorded/Replace target choice remains
separate from creation defaults. No FD transport, second codec, environment
switch, binary override or nullable native constructor argument replaces it.

## Source evidence

- Initial Python inventory: `.artifacts/native-before-ast.txt`, 1,068 modules,
  no omissions. The later integration inventory:
  `.artifacts/native-integration-before-ast.txt`, 1,075 modules, no omissions.
  Roots include Core production/tests/tools, paired Toad and metaclass_registry.
- JavaScript inventory: `.artifacts/native-javascript-before-ast.txt` uses Node's
  existing Acorn parser/visitor for the import fence, SDK loader and resource
  loader. It enumerates declarations, imports, writes, conditions and consumers.
- Syntax does not resolve arbitrary callbacks, lazy registry loading or dynamic
  MRO. Import/parser/MRO checks passed; full focused and installed checks remain.
- Required uniqueness searches: `rg 'sys.executable|native_defaults|settings.binary|self.agent_bin|self.agent_args|NativeThreadDefaults.from_runtime' src/agent_comms`
  and `rg 'def full|execution.action\(|max_attempts' src/agent_comms/coordinated_runtime.py src/agent_comms/selected_triage.py`.
  The first has no matches; the second identifies the shared entry and one
  attempt-limit declaration. The integration entries' production paths are
  declared by NativeRequestTools and NativeProject respectively.

Adding a declared executable tool still changes its declaration, not another
FULL/TRIAGE tool roster. A new native binding supplies its own behavior through
the existing NativeProject family. Python and Node helper transports share the
existing bounded exchange and result decoder rather than copying it.

## Remaining acceptance

1. Integrate published caller/acquisition/shutdown and P55 checkpoints normally.
2. Migrate the affected fixtures without discarding fsync, failed-launch,
   unsafe-source or explicit offline restrictions; batch focused checks/ratchet.
3. Rebuild the native artifact for alias identity, then verify the exact wheel.
4. On fresh isolated roots: saved-history fork using the selected installation;
   actual TRIAGE→FULL with tools and a nonempty response; default and disabled
   discovery without duplicate Core initialization; distinct settings across
   restart and ACP startup. Capture actual process identity/liveness before
   retained-owner capture. A socket file is not readiness.

The failed 693f root remains immutable and had zero submitted inputs. Parent
core-goal-retry established the startup failure from its terminal traceback:
transcript replay raised ObserverDied because publication_revision treated a
RecordedThreadIncarnation as a Thread. Fix 940999d84 addresses that cause.
Shutdown released the exact owner; the old driver mistook a stale socket for
readiness. Parent owns the fresh capture/restart acceptance; do not duplicate
that run. UNKNOWN provider, wire and file attempts are not replayed. The old FD success does not accept the
JSON replacement. No default installation or successful provider journey is
claimed by these source edits.
