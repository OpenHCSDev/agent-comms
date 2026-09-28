Fix cold saved-session compaction before input and size the committed context using its complete representation.

Cold NativeSessionPreparation shares normal launch, capability attestation, strict reopen and child retention, without sending a prompt. Both adaptive and manual journaled compaction use the actual selected model/settings. Native trigger reads actual stored context instead of requiring cached runtime usage. Removes stale contextTokens, keep_recent_tokens and allow_split_turn APIs and migrates callers.

CompactionPolicy owns retained-message sizing, complete file annotations and final summary accounting. Whole-turn and combined split-prefix summaries must reopen within the selected model policy. No raised hardcoded cap, input replay or history deletion.

Local acceptance: real copied 143686055-byte UX history through actual native CLI, ACP/router and local HTTP provider with substantial 15KB summaries: manual and adaptive both pass (83.76s). Installed wheel adaptive also passes (40.77s), preserves 601 read/211 modified file details, reopens usable context, completes exactly one new input and returns idle. Direct native whole/split cases save and reopen successfully. Focused protocol/native/backend/manual receipts included; failed intermediate runs retained. No paid provider test calls. CI deferred by owner. Live activation is a separate next step.
