# Selected handler consumption correction

Previous reviewed scope/receipts: e61afcdf, retained unchanged. New production
checkpoint: bc27f626. Correction: **19 deleted / 27 added** across existing
MroDispatch and DurableTurn; child retirement source remains byte-identical.

MroDispatch owns selection and consumption. Async dispatch selects its declared
C3 handlers once, skips unhandled values and calls the consumption hook. Default
async and synchronous consumption share the exact-type replacement validator;
None remains the existing unit-result contract, not domain/lifecycle state.
Specific-before-base ordering and consumer overrides remain declaration-owned.
DurableTurn overrides only consumption: those SAME selected bound handlers enter
original Coordination.run_async and consume synchronously under using_attempts.
It no longer probes declarations or re-enters dispatch for the same event.
Nested phase dispatch selects the distinct phase value once inside that resource.
Selected handlers are operation-local capabilities, not a stored catalog/cache/queue.
No new class, registry, worker mechanism or semantic state was introduced.

Whole family read: async ACP request/update/activity consumers, TrackedTurnSession,
SelectedParticipant, compaction/turn publication; sync failure/goal settlement and
AST debt consumers; inherited TurnProgress/OriginGoalSettlement. Before/after
existing NRA AST covers 311 production,357 test,53 tool modules with zero parse
failures. Dotted/dynamic receivers and duplicate test-local names are explicit
limits; nominal inheritance alone is not dynamic dispatch proof. No external/native
source changes. Source family output: dispatch-family-before-after-ast.json.

## Final changed controls

Normal small wheel: .artifacts/wheels-dispatch-consumption536/agent_comms-0.1.0-py3-none-any.whl
SHA25610a2bb96e773b8ab8c463ca2cc4e5739a50b1ceea3333d19ef7aa37a095eb053.
All311 installed source files match; directURL/hash proof adjacent. Existing author
runtime-owned-observation536 environment advanced by normal declared resolution;
original e61 source wheel and receipts remain retained, as historical evidence.
No new environment/native build and no public/receiving327 prefix was modified.

One batch: **4 passed in1.26s**. Controls detect changed concrete risks:

- Diamond specific/shared order and consumer override retain original C3 behavior.
- Async and sync consumption pass same-type replacements to subsequent handlers,
  preserve unit results and reject wrong-type replacements through actual dispatch.
- Each actual DurableTurn root observation selects once; nested phase is distinct.
- A real SQLite EX blocker leaves the loop free to release it; joined cancellation
  preserves the committed exact fence and restores caller access. An unhandled
  event returns while EX is held without acquiring SQLite.

No six child-retirement controls, prior42MB read, provider probe or native/UI gate
was repeated. The actual396 31–51s timing is not attributed or claimed solved.
Parent owns final review; Sch owns next small receiving wheel/current327 pairing.
Pattern BOUND-2/IMPL-13: delete consumer membership probing, use the shared owner.
