# S10: pi boundary remainder

**Head audited:** `agent-comms` `main` at `fdfab5f`; re-verify at yours. **Rules:** [00-RULES.md](00-RULES.md). **Uses** A2, A13. **Step 2.**

---

## Already done

#196 completed pi's event payloads: tool calls, tool results, input and context commits, and typed response data are modeled; no code dispatches on pi's event kinds by string; every read of pi's RPC stream goes through `PiRpcChannel`. Nothing in this file redoes that.

---

## What remains

- **V1. The selected-tool broker's hand-written decoders.** `SelectedToolRequest.from_wire` (reading `call_id`, `token`) and `from_arguments` (reading `contents`, `resource`) are **deleted** and replaced by A2 decoding, with declared wire names where keys differ from field names. Both ends of the broker's socket protocol are ours: if its shape changes, the other end changes in the same PR.
- **V2. `verify_sent_full_input`'s raw row.** It reads `SELECT * FROM native_runtime_inputs` and uses the row by ten string keys. Once S12 has declared `NativeRuntimeInput` through A13, read through it and delete the raw access.
- **V3. The extension UI choice.** `pi_events.py`'s `apply` reads `session.choice` by `confirmed` and `value`. Model it as cancelled, confirmed and value variants, and delete the raw read.

Delete any legacy or compatibility code in S10's files on the way (rule 1).

---

## Guards

In S10's files: no `json.loads` outside a decode site that feeds an A2 record; no reads by string key of a shape a class models.

## Tests

- **Contract tests only for pi's formats,** which are external: the existing recorded pi streams keep passing.
- **No golden test for the broker's protocol:** both ends are ours, and pinning it would be compatibility by another name.
- **Delete** tests of `from_wire` and `from_arguments` along with them.

## Done when

V1 to V3 are done, the guards pass, and nothing in S10's files decodes by hand.

## Dispatch

> **To the agent that built pi's event family (#196):** Please finish S10 per `docs/refactor/round2/S10-pi-boundary.md`. Read `00-RULES.md` first. V1 and V3 now; V2 as soon as S12 has landed A13 and `NativeRuntimeInput`.

## Active implementation (2026-09-28)

Pascal owns `refactor/s10-pi-boundary-20260928`, tree
`~/wt/comms-s10-pi-boundary-20260928`, based on merged226 plus R0.
V1–V3 and all native child adopters are implemented in PR234. V2 uses Cicero's
NativeRuntimeInput.one from PR237; no raw native-input row remains. Pi event and
tracked-turn lifetimes use PR232's AttachedChild/BoundedRun, including the shared
repeated-cancellation fix. The unused selected-Pi guardian and fake-only API/tests
are deleted. Existing NativeEntry now owns strict startup model/thinking metadata
for Darwin's paired PR236 consumers, with no second registry. See
`evidence/s10-pi-boundary/HANDOFF.md` for actual loopback native tools, cancellation,
boundary and guard receipts. Parent owns coupled integration/quiet activation;
S9 startup consumers remain explicitly Darwin-owned until his paired closure.

## Selected-summary dry-run retirement — native sidecar, PR343

Removed the unused readiness RPC, Python command/response family, native helper
and intermediate build stage left after325. Existing selected-summary admission
owns its live fences directly; no pretend readiness envelope remains. Deleted
the synthetic-session harness and its consumer; actual pinned native slot
mutation/cancellation/reopen and installed private ACP commit/admission cover the
replacement.340 token policy and339 current-main fork work are retained. See
`evidence/selected-probe-retirement/HANDOFF.md` for deletion counts, immutable
package, exact local receipts and their limits. Parent owns final live pairing.

## Native tool declarations — native sidecar, PR344

Existing CodingTool declarations now inherit the native tool presentation contract.
The coding capability still derives its restricted admitted membership; title and
ACP kind use those same declarations with shared path behavior. Backend's parallel
kind roster/title switch and Pi event scratch fields are deleted. No native
producer, checkpoint schema or Toad AgentProcess changes. Exact scope, physical
SDK proof and passing post-upgrade native Read acceptance:
`evidence/native-tool-declarations/HANDOFF.md`. Native Read -> official ACP passed with343 d396. Parent owns final paired
installation; no CI gate.

## Native launch selection boundary — native sidecar, active draft

NativeArguments and its declaration family replace backend's four independent
provider/model/thinking readers/replacers plus the RPC-mode interpreter. Session,
turn and catalog callers share the decoded launch owner; the actual native
producer/session custody remains unchanged. Scope excludes Boyle's ratchet and
Tesla/Carver/Noether files. Acceptance and limitations:
`evidence/native-launch-options/HANDOFF.md`. Parent owns installed/live acceptance.
