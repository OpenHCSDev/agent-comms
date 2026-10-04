# MCP original turn-authority fixture retirement

Base merged611 d41601c16d74e9e3e4195b6527fb218b8fe14af9. Production receipt and
permission consumers delegate exact owner/session/turn membership to
TurnRunner.owns_turn -> registry Thread.turn_state. RegistryOwner/TurnLeaseFence
and OwnedTurn acquisition/retirement are the determining lifecycle. The old MCP
controls instead write/read removed turns.active_turns, a second authority that
cannot prove any production lease or native owner.

Migrate the complete MCP family in tests/test_mcp_relay.py and
 tests/test_mcp_acceptance.py to that original owner. Use existing
NativeBackendFixture.author_history/open_owner/original_input for the saved SDK
source, original native GetState/stats preparation, input reservation and lease
cleanup. No new fixture class, state map, compatibility attribute or production
mechanism. Receipts after settlement and from another session/old lease must
remain rejected; delayed permission loses on actual terminal/successor lease;
controller absence/disconnection/forged token remains denial.

The real native MCP acceptance's read assertions derive turn membership from the
same current owner; they must not resurrect the removed dictionary. Its provider
and optional physical UI controls are separate and will not be repeated solely
for read-assertion migration. Existing recorded child-pipe tests remain explicitly
protocol fixtures, with executable-trust substitution confined to those tests,
not the acquired saved-owner controls. The subscriber token control can supply
a bounded permission operation at its dispatch seam, but must acquire/retire the
actual turn and use the original socket server/client; it is not a provider or
native dialog emission claim.

Patterns BOUND-2/IMPL-13. Existing NRA AST before.json captures production/tests/
tools declarations, writes and lexical consumers, with omissions and dynamic
limits explicit. Final checks follow the complete source batch, using a fresh
named execution lease on an existing holder, no environment/native build/copy,
public writes or paid provider. Originals/UNKNOWN and previous611 negative data
stay protected. Historical13/98s and physical04 EOF remain open, unattributed.

## Working batch

All MCP active_turns writes/reads are deleted from both callers. The three
saved-owner controls acquire original native preparation and real turns through
NativeBackendFixture; actual retirement and distinct successor acquisition
replace dictionary mutation. Delayed permission uses an owned AsyncExitStack
for cancellation/join even on control failure. Subscriber attachments use real
canonical_agent/SessionLifecycle instead of SimpleNamespace session/transcript
facades in both relay and native acceptance consumers. Native acceptance derives
busy/id from TurnState; its actual model/tool execution remains unchanged.

Only the old child-pipe protocol controls explicitly select native_rpc_fixture.
The acquired-owner controls do not patch launch/attestation/native writer or
trust. The subscriber dispatch supplies one bounded permission operation;
RuntimePromptRequest still owns token selection, original socket permission
request/response and denial on disconnection, while the operation acquires and
settles the original reservation/lease. Zero native prompt/provider is the
intended scope, not a simulated native tool execution claim.
