# Selected summary accounting and end-to-end admission

Owner: Arendt. Current baseline Core415/installed native776. Draft investigation and reproduction in progress; parent owns paired activation.

## Confirmed failure and contract

Real openhcs-helper prompt started 2026-09-29 22:07:15 UTC. Native retained file is about 42.8 MB; selected model window is 272000, reserve 16384, keep-recent 20000. The old ACP view received 11691 summary-liveness events and was abandoned on reattachment. The new view received the terminal typed Not sent failure: Summary provider exceeded the native plan output token budget. Journal operation d299e00ed1684f26807395ee97fb5516 is UNKNOWN, owner idle, original yo input not sent. No attempt is replayed.

Pinned Codex adapter buildRequestBody does not submit maxTokens/max_output_tokens. Its shared Responses decoder reports usage.output including usage.reasoning, which Pi Usage explicitly declares a subset of output. The selected RPC guard compares this total to the summary-retention output budget. The actual failed terminal numeric usage was not persisted, so this receipt does not invent its breakdown or provider latency.

## Maintenance relation and scope

Trace native context estimate and selected current-branch preparation through source chunk planning, summary output, provider options/usage, journal settlement, original binding and ACP/Toad terminal feedback. Existing CompactionPolicy owns retained summary sizing; existing completeSummarization is shared by compaction and branch summaries. Extend those owners for reported retained-output accounting, delete the duplicate selected-RPC budget check, and preserve terminal stream attestation/full cost usage. Do not bump output caps, create another budget store, replay uncertain work or substitute progress FPS. IDEN-5/BOUND-2: distinguish billed output from retained continuation output at the declared accounting boundary.

Acceptance: bounded offline plan extraction from an independent persistent copy of the real large saved history, actual native/Codex-adapter localhost provider reproduction with reasoning-heavy usage, true retained-output overrun negative, journal/original-input preservation, and actual installed ACP/Toad summary/terminal feedback through existing206 infrastructure. No private session, auth or full provider payload is published. Mendel owns414 legacy adaptive tests; Schrodinger owns207 history projection. Production/native activation coordinates with parent.
