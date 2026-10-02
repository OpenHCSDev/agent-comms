# Selected turn context: original S5 consumer closure

Owner: Singer. Source baseline: `d51100ec`, including merged 503 and 507.
Implementation and receiving validation are in progress; this is not an S5
completion or live activation claim.

## Existing owner search and concrete gap

`ContextSegment`, `InstructionSegment`, `TurnContext`, `RenderedInput`,
`InputContributionCoordinates`, and the native `NativeInputClaim` already own
rendering, original provenance, byte ranges and SDK token measurements.
`ContextObserved` carries the original text-free SDK manifest; `WireLog.record_context`
publishes it on the original wire. No replacement assembler, prompt store, token
formula, source registry or cursor authority is needed.

Ordinary `OwnedTurn` assembles these contributors and `TurnProgress.observe_context`
publishes native observations. Selected TRIAGE instead dispatches to
`SelectedParticipant`; selected FULL dispatches through `SelectedAttempt` to
`DurableTurn` and that same participant. Neither selected receiver handles
`ContextObserved`. `SelectedPrompt` concatenates the selected frame, awareness,
action and response instructions without contributor coordinates. Thus ordinary
path inspection does not establish the original per-turn S5 contract for selected
channel batches or direct mandatory work.

## Implementation boundary

Reuse the existing context and event families for both selected stages. Move the
existing selected instruction literals into owned instruction files and retain
their exact rendered bytes. Carry original source and owner provenance through
the same native prompt command and original input claim. Publish each observed
manifest once, using the original leased turn identity. Preserve assignment,
reservation, native proof, outcome and cursor ownership.

Einstein granted `SelectedPrompt` methods, `SelectedRequest.reserve` contributor
forwarding, and the selected context handler. Arendt owns admission, lifecycle and
async resource custody; shared transport changes require his exact method grant.
No WireLog, carry, compaction-policy or frontend edits are in this scope.

Source reasoning and coherent implementation precede batched final validation.
The final installed receiving journey must cover pure-channel TRIAGE to FULL and
direct FULL, original manifests, contributors and CLI historical inspection.
No original failed input is replayed. Until that journey is recorded, this draft
claims source work only.

## Original goal remains open

This closes a concrete selected-path S5 gap, not the entire retained-task goal.
S1 task-aware decisions, S3 measured provider reuse, and S4 repeated-compaction
recall/default disposition retain their original requirements and named parent
dependencies. S2 retention and S5 phase 2 have existing authored owners and
receiving proofs; they are not replaced by this inspection work.
