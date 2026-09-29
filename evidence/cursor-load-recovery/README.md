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
