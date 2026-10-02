# Installed completion — 2026-09-28

ACP startup, channel/DM feedback, blocked UX recovery, native reader admission,
and PF1–PF5 are merged and installed. Current detailed checklist:
`/home/ts/.local/state/agent-comms/post-feature-refactor-completion.md`.

## Idle CPU acceptance

Core PR221 (`ab98c1993013d659fb7cdf8d4c941225cb7cf5f9`) removes repeated
package/cohort processing on unchanged idle observations. Native send authority
checks remain on real work. No polling delay increase.

- Actual original owners, five-second interval:74.0%/73.0% of one CPU core.
- Installed/restarted only when idle:1.4%/1.2%.
- Fresh actual #comms input86 explicitly addressed UX; reply87 contains the unique
  requested marker. Responding observed at1.931s, Responded at7.664s.
- Postreply five-second interval:1.0%/1.2%; both idle, same owner PIDs across sample.
-37 focused local cases; copied actual root settles without repeated scans.
- User Toad was not stopped; no historical UNKNOWN inputs replayed.

The first observer expected both owners to be assigned despite an explicit UX
mention. It waited unnecessarily after UX completed. It was interrupted; the exact
same input/reply and current idle CPUs were verified read-only, without resending.
Original observation retained in installed-idle-after.json/log; corrected acceptance
in installed-idle-confirmed.json. The helper now waits for the selected UX owner.

## Reproducible runtime

- Core pin:ab98c1993013d659fb7cdf8d4c941225cb7cf5f9 (merged221).
- Toad pin:0f093ea9b23eca2c198c8900ed2c5ff057c213d2 (merged104).
- Textual pin:4fa6a9c440eaaa7dfaad45a33af146fc4b7e922e (current remote main).
- Toad local lock and tracked stack lock resolve. No unrelated package upgrades.
- Installed runtime: /home/ts/.local/share/agent-comms/runtime-acp-extensions-20260928.
- Core/Toad imports and launch symlinks resolve there.68 installed dependencies
  checked compatible. Actual native four-tool acceptance is recorded separately.
- CI explicitly deferred by owner. No shared main reset/clean/commit.
