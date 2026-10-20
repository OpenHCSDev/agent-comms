# P79 — explicit caller at every tool entry

Owner: core-native-runtime. Branch: fix/p79-native-caller. Core PR #1078. Source complete; installed native acceptance pending.

## Installed cause, not inferred source behavior

Runtime: toad-1167674eb-core-9ed8e0560-native-6468a1979d983e9d-20261016T224432Z.
Native artifact: native-current-6468a1979d983e9d/node_modules/@earendil-works/pi-coding-agent/agent-comms-extensions/global-agent-comms/index.mjs.

- Installed extension line 34 captures AGENT_COMMS_THREAD.
- Installed extension lines 143–149 invoke `agent-comms invoke --tool … --arguments …` without --from. This is where the native caller is dropped; this path is CLI, not runtime RPC.
- Installed cli_commands.py:724 defaults its explicit --from field to None; :741 binds ToolCaller.from_actor(None), yielding NoThreadCaller. ToolRequest.invoke and invoke_tool also have unsafe caller defaults, although this particular CLI path supplies the already-wrong NoThreadCaller explicitly.
- Current canonical extensions/pi-agent-comms/index.ts:179 already includes --from from its once-captured currentThread. The installed artifact is stale. Its hash f09724… matches the native lock, while native-import-manifest.json still declares the old 6470-byte / 385ce3… snapshot. Current canonical source is 6528 bytes / 28567c…. A new native package is required; reusing the frozen artifact cannot install this fix.

## Work

The existing entry adapter captures the owner name once and carries it through --from. Refresh its declared immutable snapshot so the native package actually contains that code. Require caller on both Python APIs and actor on the context-action boundary. Migrate 59 operator calls plus the publisher utility and one declared-tool call explicitly; retain the already-explicit module/function partial bindings instead of replacing them with another default. Python enforces the required keywords. The new argument-binding test and the old factory-instance test were removed under Tristan's no-busywork-test rule; neither proved actual caller delivery. No Core environment fallback, default caller or refusal catch. Both failed core-retry calls and live-bus state remain untouched.

Production delta: src/agent_comms/tools.py +3/-3, net 0 lines. Combined P79 tests +70/-70, net 0 after removing the trivial argument/factory assertions and redundant catalog uniqueness assertion. The other production-format change is the two derived snapshot fields in stack/native-import-manifest.json. Existing TypeScript source already carries the fix; it is not claimed as a newly authored change.

Before/after AST: current source/tests/tools, actual installed Core and metaclass dependency, paired Toad production: 1413 modules, zero parse omissions. TypeScript and compiled extension read directly; Python AST does not parse JavaScript or establish dynamic dispatch. Evidence is retained under .artifacts/p79-*.

Local source batch before test trimming: 92 passed, 6 failed. All six fail identically against untouched installed Core 21c25f49d, at wire_log.py:512 with Private bus writer has no durable protocol marker; these are retained baseline fixture failures, assigned to core-retry's existing fixture work. No caller failure is hidden. Ratchet exits 0. `git grep -n 'caller: ToolCaller = NoThreadCaller()' -- src | wc -l` returns 0 on this branch.

Acceptance is a fresh scratch-root native thread on the reviewed installed runtime: actual comms_set_goal records the goal under that thread. Source tests and ratchet are necessary but not installed acceptance. Installs run through agent-comms install, by whichever unit the commander assigns, on either machine.
