# Selected-tool FULL runner hook (integration preview, default OFF)

This follow-on branch exposes an **internal nominal opt-in seam**, not a
working/public Pi tool. `CommsAgent(..., private_selected_tool_intent=...)`
requires the separate `SelectedToolIntent` type plus an exact private N/K root
and reviewed native package; ordinary workers never construct the token.
ACP metadata, messages, prompts, and model text cannot activate this seam.
Without it, the FULL and triage prompts/launches retain their original
no-tools behavior byte for byte.

Only for an engaged selected FULL claim, after reserving the exact native input
and binding its prompt, the owner derives `WakeAdmission` from the committed
source, selected K, current turn/epoch, execution/fence and fresh operation ID.
It calls the separately owned `selected_tool_mode_for_owner(...)` to create a
bound `SelectedToolMode`, then passes that mode to native Pi. Triage never gets
one. The gated FULL instruction mentions at most one `selected_claimed_write`
request for a bounded existing file and forbids shell/generic edits. An
operator-preplanned file intent and the model-selected tool are mutually
exclusive. This hook does not infer input completion from any tool call.

The broker, extension, actual native tool routing, once-only ledger, per-tool
terminal receipt, and Pi tool-call authentication are **not in this branch**;
PR108 owns them. Provider-free tests inject a *nominal stub* to verify runner
ordering and authority fields, default/triage isolation, observer/forged
input/stale-epoch denial and no retry of an uncertain native result. A stub
cannot prove authenticated IPC, Pi capability exclusivity, a successful
model-selected file write, or production safety. No provider/live usage or
protected-host activation is authorized. Shared `claim_admission` comes from
the separately reviewed default-OFF operator PR once merged into main.
