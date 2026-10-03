# Ready: one replacement binding owner

The compiled native RPC consumer loses **12 lines, adds zero**. Its four successful
replacement handlers now rely on AgentSessionRuntime.finishSessionReplacement;
the original host callback and initial startup binding remain. This removes a
second bindExtensions/session_start/resources_discover/subscriber setup per
replacement. Cancellation and runtime replacement fences are unchanged.

The normal immutable candidate is 2bfb6ba8fd3b3b3b. Only RPC bytes differ from51b;
19,189 other deployment files are byte equal. The normal stock/full-tree/import
receipt is `../runtime-rebind583-native-20261003/artifact/artifact-receipt.json`.
Installed source equality and wheel hashes are in `installed-source-proof.json`.

Actual no-provider final batch: **2 passed in 6.68s** (`installed-control.log`).
The immutable CLI and supported SDK runtime both execute switch, clone, fork and
new_session. SDK observation has one startup event and exactly one event for each
successful replacement. Canceled switch leaves original identity, no extra event
and no target file. Each actual native child is retired by the original bounded
resource owner; stderr is joined empty. This is native/SDK/RPC acceptance, not UI
or configured-provider latency acceptance.

The source is an original completed 1,424-byte native localhost session retained
from581. ForkSessionHelper/SessionManager.forkFrom creates each private source;
the actual active SDK get_fork_messages response supplies the fork-before USER
entry. A saved metadata tail is valid for clone-at, not fork-before. Original
donor bytes/hash remain unchanged; new input count and provider requests are zero.
Actual identities/events and exact source hash are in `installed-acceptance.json`.

The initial unapproved external extension was correctly rejected by the immutable
import fence; retain that refusal and subsequent incorrect-control operands in
the named negative logs. No trust manifest, package payload or allow-root bypass
was introduced. The SDK observer uses DefaultResourceLoader.extensionFactories;
it is distinct from the production CLI discovery control.

The existing test host previously supplied a fake object with a no-op
setRebindSession and ad hoc disposal. It now uses createAgentSessionRuntime,
createAgentSessionServices and createAgentSessionFromServices. Shared helper
callers are backend_native_watchdog, extension_ui_native, stack_inbox_output and
stack_settlement_boundary; their test protocol/API is unchanged and replacement
resources now belong to the real SDK host. The replaced 9-line helper construction
is deleted; no second registry, state copy, parser or lifecycle family is added.
Optional Python parameters are absent external test resources, not domain state.

Whole native AST evidence remains268 modules/no parse omissions; embedded browser
JavaScript strings/dynamic resolution are not asserted as parsed runtime coverage.
Relevant catalog: IMPL-5 repeated implementation, TIME shared resource lifetime.
Actual runtime/InteractiveMode/CLI extension replacement consumers were read.

Historical 13.562/13.696s preparation gaps and 98-second selected provider spans
remain separate. This deletes demonstrated repeated work without a controlled
whole-latency claim. No public/default/root/config/input mutation or replay.
