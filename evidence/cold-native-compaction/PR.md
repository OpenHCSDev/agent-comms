Fix cold saved-session compaction before input.

Live agent-comms-ux failed after runtime restart: adaptive compaction skipped absent runtime usage and absent retained Pi child, then native deferred context refused the prompt. The earlier attachment/admission receipt did not cover this cold input path.

NativeSessionPreparation shares TurnSession launch, capability attestation, strict reopen and child retention. It starts no prompt and supplies the actual selected model to the journaled owner. Both adaptive and manual compaction use preparation. Cached UI usage is no longer required for selected-native compaction. The native decision protocol followthrough is paired with this change (Wegener).

Retained-history acceptance now starts with no child or runtime cache and exercises manual/adaptive ACP routes with actual saved history, native CLI and loopback HTTP only. Test is in progress, not a readiness claim. Backend settlement checks: 4 passed. CI deferred. No original user input replay and no live mutation at this checkpoint.
