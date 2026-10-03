# Tracked native settings ownership

Base: merged574 `d3ed99d5773528a2ff061205260f15712de10d09`.
Same checkout and existing534 installed holder; native915 remains immutable.

`NativePiRpcLaunch.tracked` publishes `_NATIVE_SETTINGS` with an atomic replace,
file fsync and directory fsync on every TRIAGE/FULL attempt. This JSON repeats
the pinned `SettingsManager` no-retry policy and `AgentSession` journaled
compaction policy. The existing native getters force agent/provider retries
off for `AGENT_COMMS_NATIVE_CONFIG_DIR`; the session refuses unjournaled
automatic/overflow continuation and stored-context input independently.
`ContextBudget.compactionRequired` uses the actual reserve, not enabled.

Remove the Python semantic policy and its publication. Keep the private agent
directory as a writable resource, with original canonical/private validation and
directory durability. Original auth/model discovery remains in RestartEnvironment.
Do not copy credentials or modify original/project settings. Native settings
remain configuration, not a second Python policy authority.

Read all retry/automatic-compaction consumers before editing, and retain AST
declaration/consumer evidence through NRA Package and native Acorn. Patterns:
TIME-7 copied defaults; IMPL-12 duplicated lifecycle work. Dynamic references
are not resolved by lexical AST evidence. Final qualification must exercise
the real pinned settings/session owners with conflicting retry configuration,
actual tracked native/ACP delivery and child retirement in the existing holder.
No native build, new environment, public input or failed-input replay.

This removes known per-attempt work; original13.562s interrequest and98.141s
provider spans remain original observations, not attributed speed gains.
