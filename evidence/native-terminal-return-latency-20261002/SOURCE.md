# Native request completion to acquisition return

Read-only source investigation after merged542; no production patch, new environment, provider call or replay. Mendel owns integration/coordinator/registry. Einstein has the terminal/proof/child-retirement seam.

## What the original clocks actually measure

The original helper2 input records8.450274s from the parent observing the model request's `finished` sample to acquisition return. The499 input records15.819279s. Native-to-parent delivery of those samples is only2.593ms/2.284ms. Provider callback totals are13.038ms/8.369ms, with maxima8.207ms/2.748ms. Exact original diagnostic hashes and timestamps are in original-tail-spans.json.

RequestProgress is a measurement, not native turn completion. Its `finished` sample precedes the SDK agent's end/post-run/settlement lifecycle. PrivateSendAdmission records acquisition after TrackedTurnSession.complete has retired custody and before its subsequent verify/commit/publication. Those later coordinator operations cannot explain this particular interval.

## Existing owners and consumers

- SDK AgentSession awaits agent event listeners, post-run work and extension `agent_settled` handlers before emitting the settlement event.
- TurnSession owns native event/watchdog/transport observation. TrackedTurnSession consumes its existing nominal handlers, awaits SelectedAttempt.observe_event for nonterminal observations and derived updates, sets finished at AgentSettled, then calls result/context_proof.
- SelectedAttempt dispatches the original event to DurableTurn and SelectedParticipant. MroDispatch already returns immediately when no declared handler applies; it does not open coordinator resources for every unknown event. Mendel owns those publication consumers.
- NativeContextProof corroborates the original emitted input/context receipts against NativeInputEvidenceRead, borrowed from the original input-commit resource. The reader verifies original bytes and decodes appended input records; NativeContextJournal selects an indexed row. There is no second context-proof authority to replace with a cache.
- NativeCustody/PiSessionChild owns retirement. ChildProcess has the sole identity-bound stop plan and joined cleanup; its executor scheduling, group retirement and pipe cleanup are part of the return interval.
- The ordinary Pi AgentSettled.apply statistics request is not the selected tracked handler: TrackedTurnSession dispatches its own settled method. An extra ordinary stats RPC cannot be assumed here.

The existing refactor-audit Package parser maps all311 Python production modules, with zero parse omissions, and finds one declaration each of TurnSession, TrackedTurnSession, NativeCustody, PiSessionChild, ChildProcess, NativeContextProof, NativeEvidenceRead and PublicationMeasurements. before-ast.json preserves the lexical consumer and inheritance leads. Native JavaScript/deployed extension sources were read directly; Python AST does not parse them and dynamic dispatch is not proved by lexical evidence.

## Extension source custody

The stale .agent-comms/extensions checkout invokes idle activity synchronously even for managed turns. It is not established as the resource loaded by these originals. Both threads currently declare the OpenHCS project, and default global discovery resolves ~/.pi/agent/extensions/agent-comms. Its current source and the9f12 manifest-selected compiled extension already skip all managed activity/release hooks. Main has that deletion since56d362f2. No duplicate patch to either checkout/global settings is justified. The original loaded artifact at those historical spawns is not retained in these request timing rows.

## Precise remaining evidence

The old request stream has no separate timestamps for AgentSettled receipt/observer completion, terminal proof corroboration, or child retirement. Therefore it cannot assign the8–16s to one of these operations. Mendel has been given this exact boundary and source chain. If a future normal user turn needs attribution, use the existing PublicationMeasurements object at these original operation boundaries; no second clock/store/authority or repeated provider benchmark is needed. No latency improvement is claimed from this source read.
