# Original input-drain fixture retirement

Base main610 e1c399d7 contains merged609/605/603. Existing NativeBackendFixture,
Native SessionManager source authoring, OwnedTurn.begin/reserve_input/open_stream,
InputDrain bind/finish custody and original RegistryOwner/TurnLease carry setup.
The old drain fixture invents saved history, witness identity/revision, removed
active_turns and a NativeCompactionWriter.verify bypass. Those are not accepted
native/shutdown evidence and will be deleted with all input_source_cases callers.

Preserve relevant versus foreign ingress, exact future queue acceptance, steer,
clear, promotion, shutdown, owner/turn loss, changed/deleted/bound queued originals
and prior UNKNOWN refusal. No replay or production compatibility path. Production
OwnerCompactionCommit receives current pending_input_keys tuple and a witness
produced by original SDK acquisition; journal membership remains original.

Patterns BOUND-2 and IMPL-13: fixture consumers use the actual original owners,
not a weaker parallel authority. Existing AST before.json names declarations and
all lexical consumers across src/tests/tools, with omissions/ambiguity explicit.
Final bounded installed changed-family check uses released existing540 only after
original package archive and fresh grant. No new environment/native/provider or
public input. Historical13/98s measurements are not acceptance of this fixture.
