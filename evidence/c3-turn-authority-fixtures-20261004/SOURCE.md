# Canonical turn-authority fixture migration

Base: actual main `d77e2dc08f445cc6f8695b2cb879994656f429a1` (includes merged #631).
Production/stack are also byte-unchanged from the earlier F4 main
`7a5c5fc2e79f2e32e4f340eaba573bf1bfec69bf`. #631 fixture changes are inherited
main history, not part of this PR or an edit of #632.
Draft: Core #633. Source-only working checkpoint; not installed or Ready.

## Owner and consumers

The original `RegistryDocument` stores each `ActiveTurn` and exact admission/turn
identity. `TurnState` derives busy and phase behavior. `TurnRunner.owns_turn`
observes that declaration; its task, lock and inbox collections are acquired
resources, not a second busy declaration. `OwnedTurn.acquire` registers original
lease and input cleanup before worker delivery. Its original exit stacks retire
those resources; consumers must not clear a test-owned active map.

Original NRA/refactor-audit `Package.load` parsed all 311 production modules and
365 test modules without omissions. The before projection found **19** retired
map accesses, entirely in the six assigned modules. Source semantics were read
through `TurnRunner`, `OwnedTurn`, `TurnProgress`, `NativeSessionPreparation`,
`InputForwarding`, `InputDispositions`, `RuntimeRequest`, `GoalScheduler`, original
session/registration owners and the shared native fixture. No production change.

| Consumer | Replacement and concrete contract |
| --- | --- |
| test_turn_runner | SDK-authored saved history; original ACP/native acquisition. Two real session leases isolate cancellation. Original provider hold exercises shutdown joining and child/group retirement. Pre-write faults preserve their exact cause and NotSent originals. Native 503 preserves one actual STARTED input without retry. |
| test_acp | Compaction and settings use `NativeBackendFixture.original_input` and its registry lease/inbox. Original settings-response owner still decides confirmation. Tool/thinking/activity forwarding uses actual SDK/native bash and the existing localhost response provider, deleting the scripted capability/event child. |
| test_acp_private_nk_delivery | Busy native owner refuses another selected execution; original lease retirement allows actual TRIAGE/FULL. This case no longer bypasses package trust or fakes TrackedTurnSession.execute. Other independent delivery protocol cases retain their existing explicit scope. |
| test_acp_input_disposition | Saved SDK owner, actual Pi RPC ACK and exact native STARTED. Retirement of the original private child before queued start preserves UNKNOWN. Late subscriber/history dismissal never changes its evidence. A bounded actual controller hard exit after acceptance preserves the unbound input; canonical stopped restoration/acquisition replaces invented process registration. No empty history, fake capability, stub native child, direct binding-map restoration or source PYTHONPATH substitute. |
| test_runtime_goal_retry_running | Real SDK/native input IDs and starts. Only delivery of the original terminal stream is held/interrupted to exercise success, error, EOF, exception and cancellation. Registry Publishing remains busy until original lease retirement. Goal attempt and origin fences remain original; owner-loss uses unregister, not an invented PID. |
| test_stack_retry_running | Same saved native owner/provider and actual owner socket. Deletes alternate HTTP server, model/auth/preload construction and fake busy reads. The original response gates prove deferred goal launch, distinct leases, pause and no replay. |

`NativeBackendFixture` now owns shared saved-owner attachment and native argv
construction for its run/open consumers. Runtime/auto-wake options are original
CommsAgent configuration, not copied turn state. Optional ACP client is an
external controller resource: `on_connect` consumes it before saved acquisition;
required native observation remains unconditional. `LoopbackProvider` adds one
optional original reasoning delta alongside its existing tool-call response.
This is controlled external response data, not native proof or turn authority.

The input ledger's deliberately retained unconfirmed reservation is neutral
historical evidence. No native STARTED, receipt, incarnation or actual byte send
is inferred for it. Current native starts in the migrated retry/queue cases come
from the original SDK/pipe. Existing removed-facade guards remain intact.

## Verification boundary

Eight changed Python files compile as source; no imports, test processes,
providers, package installations or native executions were performed. Production
and stack are unchanged. The working source deletes competing fixture authority;
these statements do **not** claim runtime acceptance or historical latency gain.

After source review, one installed changed-family batch needs a separately
archived/cleared/granted existing 540 purpose and the exact source-declared
immutable native package. No loan is assumed. Select the changed test nodes only:
turn isolation/shutdown/failure; compaction/settings/tool publication; selected
busy admission; queued ACK/STARTED/UNKNOWN plus hard exit; goal retry terminal and
owner/origin/attempt fences. Unchanged suites, full SDK qualification, provider
waves, public inputs, original UNKNOWN replay and new environments are excluded.

Static member references resolve through the existing fixture/owner classes.
Monkeypatched fault/delivery callbacks and external native JavaScript dispatch
are dynamic boundaries: Python AST does not prove their runtime resolution.
Relevant original SDK OpenAI reasoning and bash progress source was read; no
JavaScript AST or installed-behavior claim is made by this source checkpoint.

## Prepared installed batch (not executed)

The retained Core421 filewheel `d96ab5bb53e360591d8cff8e1e7eb85559eb7ac985c3047f6ffaef80f0dba935`
contains all **339** Git-owned package members (311 Python + 28 resources) and
three forced assets equal to this source. `pyproject.toml`, README and LICENSE
also equal its determining build commit. `retained-wheel-source.json` records
that complete equality; no rebuild is needed. Native manifest is exact 086.

`installed-controls.py` reuses the original #611 installed runner, changing only
its wheel operand. It imports application code from the granted prefix first,
then appends the existing system test runner and adds the test-helper directory;
no production source overlay or dependency installation. `changed-nodes.txt`
selects the replaced family only. Pass `-o addopts= -q --basetemp OWNED_NEW_OUTPUT`
to disable project-default parallel/coverage options. This is one serial batch,
not the whole ACP/private-delivery suite. Recheck headroom immediately before it.

Both new Bohr540 package/execution and immutable086 read grants are required.
The controlled native requests exercise actual input ACK/STARTED/UNKNOWN and
retry settlement, so this batch must not be reported as zero native inputs.
Only localhost controlled provider responses are used; no external provider or
public input is authorized by this batch. Preserve failure originals and retain
terminal/proof/cleanup receipt before handing the holder back.

## Current typed failure and controller consumers

The original OwnedTurn producer reports/publishes the original fault, then raises
PromptFailureReceipt.request_error. Fixtures now decode that existing typed
receipt and assert exact chained causes, NotSent/Started and published feedback;
they no longer demand raw exceptions from the public ACP result or raw private
exception text where diagnostics intentionally redact it. Real localhost 503
still preserves the original provider detail and exactly one native Started.

RuntimeProxy.subscription supplies the actual controller token. Late socket
attachment occurs after the original first input starts but before its distinct
followup reservation. Retry socket prompts use the same acquired subscription;
updates decode at the external dictionary boundary. Queued native retirement
requires a typed unconfirmed failure, never synthetic successful ACP settlement.

Tool-running/idle observations come from TurnChangedUpdate/Registry TurnState,
not another expected activity.jsonl Working append. The selected busy fixture
now enables the actual private runtime/wake configuration before expecting
TRIAGE/FULL and sends while the original busy lease is already acquired. Original
concurrent runtime observation may own that drain; assertions retain exact two
native requests and original source/lease cleanup, rather than a caller count.

These changes compile and remain source-only, with the original three negatives
retained. Production and stack remain unchanged on this fixture branch. #638
owns the separately published real native ingress custody defect; no installed
batch resumes until its explicit successor package purpose is granted.
