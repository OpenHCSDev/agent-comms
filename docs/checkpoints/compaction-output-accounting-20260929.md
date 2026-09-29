# Selected summary accounting and end-to-end admission

Owner: Arendt. Current baseline Core415/installed native776. Draft investigation and reproduction in progress; parent owns paired activation.

## Confirmed failure and contract

Real openhcs-helper prompt started 2026-09-29 22:07:15 UTC. Native retained file is about 42.8 MB; selected model window is 272000, reserve 16384, keep-recent 20000. The old ACP view received 11691 summary-liveness events and was abandoned on reattachment. The new view received the terminal typed Not sent failure: Summary provider exceeded the native plan output token budget. Journal operation d299e00ed1684f26807395ee97fb5516 is UNKNOWN, owner idle, original yo input not sent. No attempt is replayed.

Pinned Codex adapter buildRequestBody does not submit maxTokens/max_output_tokens. Its shared Responses decoder reports usage.output including usage.reasoning, which Pi Usage explicitly declares a subset of output. The selected RPC guard compares this total to the summary-retention output budget. The actual failed terminal numeric usage was not persisted, so this receipt does not invent its breakdown or provider latency.

## Maintenance relation and scope

Trace native context estimate and selected current-branch preparation through source chunk planning, summary output, provider options/usage, journal settlement, original binding and ACP/Toad terminal feedback. Existing CompactionPolicy owns retained summary sizing; existing completeSummarization is shared by compaction and branch summaries. Extend those owners for reported retained-output accounting, delete the duplicate selected-RPC budget check, and preserve terminal stream attestation/full cost usage. Do not bump output caps, create another budget store, replay uncertain work or substitute progress FPS. IDEN-5/BOUND-2: distinguish billed output from retained continuation output at the declared accounting boundary.

Acceptance: bounded offline plan extraction from an independent persistent copy of the real large saved history, actual native/Codex-adapter localhost provider reproduction with reasoning-heavy usage, true retained-output overrun negative, journal/original-input preservation, and actual installed ACP/Toad summary/terminal feedback through existing206 infrastructure. No private session, auth or full provider payload is published. Mendel owns414 legacy adaptive tests; Schrodinger owns207 history projection. Production/native activation coordinates with parent.

## Verified native checkpoint

The actual 42817440-byte source selects 538709 history bytes (five map segments) and164741 current-turn bytes (two map segments), with four workers, a191712-byte request budget and4096 retained output tokens. Offline preparation took0.72s. The plan excludes earlier history/metadata, so the raw42MB size does not explain provider duration. Liveness sequences count provider deltas, not segments or completed tokens. The mid-stream model-setting request selected the same model; no causal model change is established.

The immutable native776 / installed Corec1d baseline reproduces the defect with20 retained tokens plus12000 reasoning tokens: four concurrent map requests join, then a budget refusal preserves the saved source and unbound original. The candidate runs the same real source through the actual Codex decoder and shared native pipeline in nine requests, links one summary commit, and consumes the distinct fixture original reservation exactly once. A true20000 retained-token overrun plus12000 reasoning tokens still refuses, retains UNKNOWN, preserves the source byte-for-byte, and binds no original. Baseline exit0 (expected refusal); candidate two cases exit0 in17.63s. These are controlled loopback calls, with no paid route or original live input.

All summary callers now use shared completeSummarization accounting and streaming. The selected RPC keeps terminal custody/attestation and joins siblings, and no longer owns a duplicate output policy. Full usage is retained for cost. Native thinking deltas extend inactivity without flooding ACP/UI; typed source/text progress is provisional. The manual caller uses that same callback. The native preparation script also stops packaging patch backup files; a fresh build verified the complete new tree commitment. Bounded touched-file dispatch ratchets and focused lint pass.

Core416 can pair with current Toad533c7f6 for the accounting checkpoint: its existing renderer accepts the shared typed extension and final summary. Progressive body painting additionally requires paired Toad209. That UI gate is pending. Its first physical /compact failure occurred before any provider call: a private copy with834 untracked older inputs lacks the required private history coverage floor. Thirty-three native tracked-input proofs do not mint owner coverage for that legacy history. The fixture is being corrected through real seeded native inputs, preserving that production refusal. The same full42MB source remains covered by the native accounting reproduction.

Einstein owns all C1 vocabulary in417; the parent owns canonical turn lifecycle. No OwnedTurn edit, global installation, live session mutation or failed input replay is included.
