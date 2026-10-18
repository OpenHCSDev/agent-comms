## What changed

- extends Pi's existing `CompactionSettings` payload budget with one immutable `retainedContextTokens: 300000` declaration
- makes `SettingsManager` derive managed sessions' effective compaction settings from that owner, so stale or per-thread `compaction.enabled: false` cannot bypass Core's threshold
- keeps Pi's existing `shouldCompact` and `ThresholdCompactionReason`; the effective threshold is `min(model window - reserve, 300000)`
- keeps provider model metadata unchanged and adds installed Core-path coverage for a 1,050,000-token model with compaction persisted off

Pi still decides and performs compaction at its existing completed input/tool boundaries and after completed responses. There is no second threshold engine or mid-tool trigger.

## Why the live thread reached about 850k

The selected runtime model is `openai-codex/gpt-5.6-sol`, whose loaded model definition reports 272,000 tokens. The similarly named OpenRouter model separately reports 1,050,000 but is not selected. The live thread had persisted `compaction.enabled: false`, so Pi treated the 272k value only as a disabled local compaction threshold; the remote Codex endpoint continued accepting requests and reported about 892k total/cached tokens. This change enforces Core's budget without falsifying either model definition.

## Ownership evidence

TypeScript AST inventory parsed 40 candidate production/declaration modules with zero omissions. The complete relevant family is Pi's compaction declaration/implementation, `SettingsManager`, `AgentSession`, branch summarization, and public exports. After the change, the numeric limit is declared only on `DEFAULT_COMPACTION_SETTINGS`; settings and trigger consumers derive it. NRA could not load in the current source checkout because its dependency registry rejected a duplicate key; the refactor-audit Python census parsed the Core patch root, and the TypeScript AST covered the affected packaged JS and declarations.

## Verification so far

- prepared and fully hashed native package: passed
- local Pi cap journey: per-thread disabled, exact 300000 boundary does not compact, 300001 does, one threshold compaction, next turn resumes from summary
- installed Core native path: 3 passed (ordinary threshold, hard cap despite disabled setting, post-compaction tool rounds)
- WIP, overflow, and threshold local journeys passed; an unrelated manual-mode assertion observed the expected dirty-worktree WIP prefix rather than a bare summary
- `git diff --check` and Python compile passed

Still in progress: safe configured-provider acceptance on an isolated fork of a large real session, followed by the refactor ratchet and final installed-runtime check.
