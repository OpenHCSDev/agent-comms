# Latest fork pins — 2026-09-28

- Textual6ac3cdd919b1bfb3333091c9ac94df6c114d9fa9: latest merged fork main, layout/paint reuse changes.
- Toad a27814f8a8cc257ded4ce267dd473c5913398918: latest merged fork main including PR105 matching Textual pin.
- Core pin unchanged. Stack manifest and lock resolve together.

Validated in a separate worktree environment, then installed exact Git revisions in
runtime-acp-extensions-20260928. Both staged and installed Toad mounted actual DM
views against live Comms history and rendered recent channel receipts. Zero model
messages sent. Installed direct_url metadata confirms both exact fork commits;
all68 dependencies compatible.

Existing user Toad was not restarted; reopen it to load the new Python code.
Disposable staged environment and UI preference artifacts removed after acceptance.
CI deferred by owner.
