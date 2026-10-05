# Original native MCP qualifier: coherent source checkpoint

Core draft682, base main38a0533e5efa12ae0be9266e6b842ac07e3769ab; paired Toad draft472 at8c84f6d40e4c0ad4515f45e83c38c9f3987a6092. Only the original Core qualifier changes. Production, dependencies, pins and native resources remain unchanged.

## Original owners and deleted consumers

Registry ActiveTurn/TurnState own identity, phase and the finished cut. TurnRunner.current_turn_update and TurnTranscriptUpdate publish that original state as TurnChangedUpdate; RuntimeProxy forwards the ordered SDK payload. Attachment.session_update and the passive Audit now consume those states. Both ignore the initial idle observation, keep the original busy cut through phase updates, and require its matching idle cut; the audit requires exactly one settlement and its one real MCP receipt. Removed both retired Started/Settled imports and all four old event decisions.

SessionLifecycle.new_session supplies the actual session identity. All three asynchronous live busy checks use the existing Coordination.run_worker; the midturn identity comes from that same acquired observation. There is no fabricated lease, terminal packet, compatibility event or production state store.

The existing external adapter selector now scopes its sibling import path through pytest.MonkeyPatch; open_observer/session_update/request_permission/disconnected remain the same contract. The original os environment is scoped inside the observer lifetime, retaining its isolated XDG paths and restoring before its teardown. Deleted the nonexistent backend.os reference. AsyncExitStack owns the canonical owner, attached ACP consumer and RuntimeProxy from acquisition; callbacks retire in reverse order after pending prompt cancellation. Setup failures also enter that cleanup.

## Preserved actual boundaries

The real MCP configuration/SDK stdio server, original trust/ledger writer, bounded loopback model and nonlocal-network guard, and real simulated-user PTY producer are AST-identical. Single echo/full outbound response, no-controller refusal, held model response during midturn revocation, actual controlling socket disconnect, mounted permission, and original MCP PID cleanup assertions remain. Paired Toad472 handles actual SDK updates and mounted permissions through current Agent/SessionView owners.

The original Package collector parsed 324 production and 373 test modules with zero omissions before and after. Source compilation passed for this qualifier and the three paired Toad controls. No application import, collection, test, prefix, native, provider or input operation ran. The genuine four-case native MCP/Toad qualification remains UNRUN and needs a separately issued exact installed source/artifact/input purpose. Source compilation is not native or GUI acceptance.

AC_MCP_TOAD_ADAPTER is a genuine arbitrary external selector; future values and their dynamic dependencies cannot be enumerated by this source trace. Other historical test modules using retired events remain outside this assigned qualifier family. This checkpoint does not qualify their old instructions or assertions.

PUBLIC471 observe01 remains consumed FAIL. Its clients returned; the separate observe02 recipe uses the original Heis cd223 recorder/1cce capture helper read-only and remains unlaunched pending actual07 source-witness return and fresh public admission.
