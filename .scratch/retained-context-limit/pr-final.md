## What changed

- makes Pi's existing `CompactionSettings` own one immutable `retainedContextTokens: 300000` budget
- prevents persisted `compaction.enabled: false` from disabling that managed-session budget
- uses `min(model context window - reserve, retained budget)` for both the normal threshold and every summarizer request
- folds oversized history through ordered bounded summary updates, including split-turn prefixes, instead of sending one over-window summary request
- classifies a completed response crossing the budget as standard threshold compaction; overflow remains interrupted-request recovery

Pi remains the only compaction engine. Provider model metadata remains unchanged, and no compaction runs mid-tool.

## Root cause and live recovery

PR #767 is on `main`, but its earlier cap only changed the threshold calculation. `agent-comms-ux` had persisted `compaction.enabled: false`, so it bypassed the local threshold and reached 926,423 retained tokens. The next provider request failed with `Codex error: Your input exceeds the context window of this model.` Pi correctly entered overflow recovery, but `prepareCompaction()` handed the entire oversized span to one summarizer request; that request exceeded the same model window and could never commit a compaction.

The live session was copied and SHA-256 verified before recovery. With the owner fix, Pi's manual compaction path compacted that saved session without sending or replaying an input: `tokensBefore=926423`, `estimatedTokensAfter=42399`, exactly one new compaction entry (91→92), and the retained summary starts with the thread's `Now:`, `Uncommitted:`, and `Next:` state.

## Source evidence

- `stack/patch-native-compaction-cap.py:43-78`: sole budget declaration and threshold derivation
- `stack/patch-native-compaction-cap.py:129-211`: bounded ordered summary updates derive from that budget
- `stack/patch-native-compaction-cap.py:213-237`: split-turn summaries use the same bounded path
- `stack/patch-native-compaction-cap.py:239-251`: completed responses use threshold reason; interrupted requests retain overflow recovery

The TypeScript AST inventory parsed 40 candidate production/declaration modules with zero omissions; the numeric 300,000 limit is declared only on `DEFAULT_COMPACTION_SETTINGS`.

## Verification

- refactor ratchet: rc 0, no changed Python production paths after current `main` integration
- Core native compaction suite: 7 passed
- local Pi journeys: threshold, overflow retry, WIP carry, disabled-setting cap, and oversized three-request summary fold passed
- isolated configured-provider fork: one `ThresholdCompactionReason`, 91→92 compactions, next-turn usage 40,645 tokens, `FORK_LIMIT_OK`; original session hash unchanged
- live `agent-comms-ux` recovery: 926,423→42,399 tokens, no input sent or replayed
