# P79 — explicit caller at every tool entry

Owner: core-native-runtime. Branch: fix/p79-native-caller. Unfinished.

## Installed cause, not inferred source behavior

Runtime: toad-1167674eb-core-9ed8e0560-native-6468a1979d983e9d-20261016T224432Z.
Native artifact: native-current-6468a1979d983e9d/node_modules/@earendil-works/pi-coding-agent/agent-comms-extensions/global-agent-comms/index.mjs.

- Installed extension line 34 captures AGENT_COMMS_THREAD.
- Installed extension lines 143–149 invoke `agent-comms invoke --tool … --arguments …` without --from. This is where the native caller is dropped; this path is CLI, not runtime RPC.
- Installed cli_commands.py:724 defaults its explicit --from field to None; :741 binds ToolCaller.from_actor(None), yielding NoThreadCaller. ToolRequest.invoke and invoke_tool also have unsafe caller defaults, although this particular CLI path supplies the already-wrong NoThreadCaller explicitly.
- Current canonical extensions/pi-agent-comms/index.ts:179 already includes --from from its once-captured currentThread. The installed artifact is stale. Its hash f09724… matches the native lock, while native-import-manifest.json still declares the old 6470-byte / 385ce3… snapshot. Current canonical source is 6528 bytes / 28567c…. A new native package is required; reusing the frozen artifact cannot install this fix.

## Work

Bind the native identity once through the existing entry adapter, refresh its declared immutable snapshot, require caller on both Python APIs, and migrate every source/test/journey consumer explicitly. No Core environment fallback, default caller or refusal catch. Preserve both failed core-retry calls and all live-bus state.

Before AST: current source/tests/tools, actual installed Core and metaclass dependency, paired Toad production: 1413 modules, zero parse omissions. TypeScript and compiled extension read directly; Python AST does not parse JavaScript or establish dynamic dispatch.

Acceptance is a fresh scratch-root native thread on the reviewed installed runtime: actual comms_set_goal records the goal under that thread. Source tests and ratchet are necessary but not installed acceptance. Codex alone builds/installs native and operates the fleet pointer.
