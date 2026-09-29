# Trusted cursor load recovery

Real live log `Agent_Comms_2026-09-28T20_02_43_268392.txt` contains 26 retained
transcript events for nra-architecture, but cursor revision 6 is empty and
the trusted load response revision 7 is unavailable. Transcript availability
and model-input verification are different facts.

The regression uses a real bus file lock, coordination store, owner process
identity, and RuntimeServer delivery over TCP. No provider call or prompt is
made. On previously installed core 4510dddf, recovery times out: the cached
earlier empty broadcast suppresses the next healthy observation. Invalidating
the broadcast cache when producing trusted load metadata lets the periodic
publisher deliver the fresh observation. No stale proof is substituted.

Red: 1 failed (recovery receive timed out), 2.68 seconds.
Green: 1 passed, 0.81 seconds. Installed and live UI verification follow.

## Installed and live

Core295 merged e6fa8feb; Toad130/131/133/135 merged through c0d61fa5;
Textual c9743801 unchanged. Immutable runtime-cursor-recovery-20260928 activated.
Installed lock/socket regression passed (1.21 seconds). Four idle owners restarted
through canonical lifecycle; all four fresh ACP initialize/load attachments passed.
Actual Toad views of existing nra-architecture and PR95 sessions reached ready,
mounted retained history and removed loading. Saved history available is visible.
A concurrent NRA load briefly reported unavailable bus verification independently;
a subsequent live view reported no current verified input. No input was sent,
no store reset, and no historical prompt replayed. Existing Toad must reopen.

Deletion: core removes no production lines (four-line cache invalidation); Toad135
removes the misleading label and redundant always-nonempty replay branch. Broader
nominal deletion remains separate owned work, not claimed complete by this fix.
