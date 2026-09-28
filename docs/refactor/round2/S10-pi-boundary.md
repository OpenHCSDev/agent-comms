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
V1 and V3 are implemented together with current socket/UI producers; see
`evidence/s10-pi-boundary/HANDOFF.md`. Full S10 remains incomplete.
V2 is assigned here and waits Cicero's A13/NativeRuntimeInput; native child
adoption in `native_pi.py` and `selected_pi_child_deadline.py` waits Lovelace's
A12. Neither dependency is duplicated. Parent owns quiet whole-step activation.
