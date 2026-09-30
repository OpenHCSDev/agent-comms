# comms428 original native wait investigation

Owner: Mendel. Base d16238a5 (merged #449); Arendt owns configuration/startup
#448, parent owns public activation, Sch owns source cursor/outcome/UI.

Original public worker 3196900/birth 24802922, turn
`a4289a93bc4547d68593af946e736c29`, admission 1004, generation 13,
started 1790774930.957728, phase ModelWait. Original native process 3262993.
Root `/var/tmp/agent-comms-live-20260927-wzjtqhza`; retained native journal
`/home/ts/.pi/agent/sessions/--home-ts-.agent-comms--/2026-09-29T23-39-56-904Z_01a0ef8a-0c69-75a1-86fe-53cd38ab0d18.jsonl`.

Original toolResult `83b1f787` at 13:35:56.167Z → assistant `b8490b7e`
at 13:38:06.065Z → edit result `41c19d41` at 13:38:13.364Z. The native
gap is 129.898 seconds, so this cannot be attributed solely to delayed UI
publication. Completed assistant usage records 1034 reasoning tokens and
2060 output tokens; its message timestamp is 1790775356207. Those counts do
not establish continuous streaming or identify provider/network/CPU delay.

The owner's eventual response does not close the latency defect. Trace the
existing watchdog/event, original prompt and child/request custody and
publication timestamps read-only. No native/provider calls, replay, cancel,
owner restart, lease clear, direct pipe consumption or semantic state mirror.
Preserve history, drafts, UNKNOWN and original attempts. Identify uncertainty
when historical stream timing was not retained. If a concrete correction is
needed, use the original event/lifecycle owners and affected installed journey.

The independent R1 original sequence-161 attempt completed normally, its
original lease retired, and later public registry reading finds R1 idle.
Exact #449 recovery receipts remain in its original persistent worktree and
scratch directory; do not rerun its completed gates.

Scratch owner Mendel:
`/home/ts/.cache/agent-scratch/comms428-native-wait-custody-20260930`.
This draft is investigation ownership, not a latency fix or readiness claim.

## Causal checkpoint: managed tools enter owner admission repeatedly

Pattern IDEN-7: an observation checks the complete reviewed native artifact,
although it needs the existing thread/project identity. IDEN-5: managed native
hooks publish an activity copy while the fenced OwnedTurn already owns phase.
The original native environment confirms PI_PARENT_ID and managed=1, together
with the exact private root/package pair. This enables three synchronous CLI
calls for every ordinary native tool: project guard (`thread --no-pending`),
working activity, and thinking activity. Native AgentCore awaits beforeToolCall
and afterToolCall before creating/publishing its toolResult. These costs are
inside the original journal's tool intervals, not merely UI receipt lag.

Each inherited CLI call currently builds wire -> LocalRoute.bind_owners ->
private_nk_launch -> validate -> _preflight -> _trusted_package, scanning the
whole package and entering the bus durability barrier. The subsequent retained
launch pin takes another bus barrier. The original activity timestamps occur
inside the observed tool intervals, immediately before their results. Edits
also enter the working hook; details over 200 characters reject the activity
record after the CLI work (the hook sends up to 240 and swallows the error).

The actual installed package verification, read-only, measured 1.249s wall /
1.242s CPU under cProfile, with 138,352,514 extra rchar bytes and 21,269 tree
visits. Separate parse-only CLI import timing was 0.480s; pure decode of the
original 272,422-byte registry was 0.093s under profiling. Thus neither registry
decode nor import startup alone explains the multi-second results.

A same-root actual private `agent-comms thread --no-pending` control measured
2.469s baseline versus 0.918s candidate with cProfile. The baseline contains
1.310s of package_tree_digest; the candidate contains zero package verification
calls and retains the real bus/registry identity checks. One unprofiled pair
measured 1.201s versus 0.479s. These are fixture measurements, not an allocation
of every second of the original 5–10s results or the 129.898s gap.

### Source and all callers

The existing PrivateNkLaunch owns external environment decoding via its class
method. Deleted the standalone private_nk_launch function and migrated its
route, entrypoint and test callers. Route observation retains the exact decoded
selection; private_nk_from_environment still validates before worker/ACP
admission. Existing owner launch, restart entrypoint and restart environment
validate again at their operation boundaries. No native trust cache or protocol
alias was added. OwnerLifecycle.pin and every bus/source durability check remain
unchanged. Managed global native activity hooks are removed; standalone
participant hooks retain their original activity/release behavior. Session
attachment and the project handoff guard remain in place.

Seven affected admission/observation controls passed in 1.82s. An untrusted
package permits thread observation but still rejects executor admission and
restart environment preparation. The actual candidate wheel entrypoint read the
private fixture successfully in 0.776s; all 290 Python sources match the wheel.
It borrows reviewed dependency resources from the existing runtime. It is not a
standalone activation stage. See evidence/comms428-native-latency.

### Historical model interval and remaining acceptance

The original assistant uses openai-codex-responses / gpt-6.1-sol. Its inner
message timestamp is created inside that provider's stream function, before
request serialization, transport selection, WebSocket/SSE handling and possible
provider transport retries. It follows the prior toolResult by 40ms. The
completed message is journaled 129.858s later. That brackets native provider
stream/request work, including any local serialization, transport, queueing and
reasoning; it does not establish continuous reasoning or a provider stall.
Core's existing MessageUpdate progress updates the watchdog, but the original
journal does not retain each stream delta's arrival/publication time. Debug
logging was disabled; read-only nonblocking py-spy was permission-denied. No
process was paused and no bytes were stolen from the original RPC pipes.

The 129.898s defect remains open pending evidence from existing provider/stream
instrumentation. Do not label it fixed by the tool-cost correction. Native e36
is untouched; the updated extension snapshot/manifest still needs a bounded
reviewed native build and actual managed tool/phase gate before whole #450 Ready.
No public activation, owner stop, prompt, replay, cancel or paid provider call
occurred. Sch owns UI compaction-before-paint; Heisenberg owns #236 warm buffers.

The postmerge #449 third-helper/original161 receipts were carried normally into
this branch at 02f9ca40, and main #445 was integrated normally. They are preserved
under evidence/r1-preparing-input-custody and are not new latency acceptance.
Scratch, wheel, private fixture and profiles remain owned by Mendel under the
named scratch directory; borrowed runtime/native package resources are protected.

Published source checkpoint: 56d362f2cd3393b70a60c53b7af964e21f461aad.
Required changed-source ratchet against normal main 4295d680 passes with zero
positive deltas. Exact deletion accounting: 40 production lines replaced / 44
added; native snapshot declaration changes another two lines each way. This
counts the moved environment decoder honestly, not as 37 deleted semantics.

The next native package build is resource constrained: current headroom assert
exits 2 (swap 14.1GiB, available RAM 17.2GiB, home free 20.1GiB). No owned native
worker remains to stop. Defer the package duplication/large native build under
this guard; preserve the original/current reviewed native package and dependency
resources. The lightweight installed CLI stage and retained receipts remain.
Whole managed native acceptance has not been claimed or replaced by an inline
extension-only fake gate. Backend source checkpoint is independently reviewable;
UI/viewport work is not its dependency.

## Bounded compiled snapshot/native SDK gate

Parent explicitly clarified that the swap warning does not block one
proportional bounded gate. Ran a 3.734s kernel-network-denied source snapshot
gate against unchanged current e36, with the installed source wheel. No clone,
new package tree, installed file mutation, public input or paid call occurred.
The manifest's source SHA256 and byte count were checked before esbuild.

Used native DefaultResourceLoader.extensionFactories, createAgentSession,
SessionManager and the actual installed beforeToolCall/afterToolCall callbacks
and built-in tools. No fake ExtensionAPI or alternate tool implementation.
A native-generated saved header was reopened; this is header-only saved state,
not retained-history, ACP, owner-turn or provider-stream acceptance. The current
project-sync factory remained enabled. The candidate comms factory registered
32 tools and only session_start; no managed activity/release hooks. Actual
read -> edit -> read changed the fixture file and returned its new contents.
No activity.jsonl was emitted. No fixture process remains.

Measured candidate before-hook/tool/after-hook milliseconds respectively:

| Native tool | Project hook | Built-in work | Result hook |
| --- | ---: | ---: | ---: |
| read | 565.444 | 3.869 | 0.388 |
| edit | 541.731 | 7.084 | 0.054 |
| read | 556.282 | 1.200 | 0.040 |

These are actual SDK component timings. They do not claim a before/after full
provider turn or allocation of the original 129.898s gap. First probe used
noncanonical edit arguments without native prepareArguments, so validation
rejected the fixture's edit before mutation; its original failure log is
preserved. Corrected probe uses the declared edits[] and native preparation.
See managed-snapshot-gate.json and the exact probe sources in the evidence dir.

### Production bundle integration requirement

The native production loader admits only deployment-manifest compiled modules
inside the reviewed package. Explicit SDK extensionFactories can exercise this
snapshot, but do not replace production discovery. A future bundle must carry
both the newly compiled global-agent-comms module and the updated
stack/native-import-manifest.json as dist/agent-comms-imports.json. The source
snapshot declaration is already updated. stack/pi-native.sha256 still selects
e36, so the existing builder must not reuse that target as a new candidate.

Parent's existing fresh-package preparation command, after reviewing/publishing
new per-file and complete tree pins from the ordinary native build recipe:

```sh
cd /home/ts/wt/comms428-native-wait-custody-20260930
mkdir -m 700 /home/ts/.cache/agent-scratch/comms450-native-package-build-20260930
TMPDIR=/home/ts/.cache/agent-scratch/comms450-native-package-build-20260930 \
  stack/bin/prepare-pi-native
```

Canonical preparation already runs prepare-native-import-boundary.py, which
runs prepare-native-global-extensions.mjs and checks declared source bytes
without importing mutable user sources. It verifies selected files and the
complete package tree before publishing a new .pi-native-<pin> directory.
Changing snapshots alone does not change the directory selector; old pins can
return the unchanged existing package or reject a changed fresh build. Review
and update the new compiled module/import manifest checks and complete tree pin
in stack/pi-native.sha256 first using the existing reviewed build/pin procedure,
then require canonical verification and automatic extension discovery against
that new package. Do not edit or repair existing e36 or bypass its commitment.
Rebuild the wheel with those matching native resources before paired activation.

The lightweight requested snapshot/managed-tool gate is complete. Whole
production bundle activation is parent-owned. Sch/Heisenberg UI work is not a
backend checkpoint dependency. Arendt now owns the separately reproduced
seq247 pre-byte BUS contention/UNKNOWN admission workflow; #450 leaves
OwnerLifecycle.pin, BUS admission and lock lifetimes unchanged, and transferred
its exact route/trust/multi-CLI timing evidence directly. Preserve UNKNOWN.
