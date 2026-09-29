## Scope and deletion

Deletes the native proof JSONL full-history startup scan and mutable generation mirror; deletes Python's repeated full proof scan. The existing NativeContextJournal declaration owns one indexed transactional `.input-proof` authority, generated native schema and exact indexed queries. Native session history remains the input claim/UNKNOWN authority.

## Working checkpoint

- Actual pinned CLI + saved session + normal TurnSession/local HTTP: increasing proof history6.5MB/39MB/137MB, cold reopen/new input3.23s/3.23s/3.31s, native peakRSS171–176MB. Exactly4new inputs, generation1 retained.
- New immutable matched package86c2; live d396 untouched.
- One-shot offline conversion implemented; production current format only. No cap raise/replay/provider spend.
- Details and failed build receipt: evidence/native-proof-checkpoint/HANDOFF.md.

## Still draft

Crash publication/UNKNOWN/migration acceptance and all direct fixture/caller closure remain active; installed actual native/ACP acceptance and parent reviewed quiet cutover still required. Separate native JSONL metadata-index rebuild is explicitly outside measured proof-growth timing claim. No readiness or live-use claim yet.

Tracks #107. Parent owns deployment. Source/worktree stays persistent.
