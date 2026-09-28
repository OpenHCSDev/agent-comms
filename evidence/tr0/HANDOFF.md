# TR0 shared ratchet — working source

Base parent229 `58bfad3`; own `refactor/tr0-packaged-ratchet-20260928`.
Moved tools/debt_ratchet.py into agent_comms.debt_ratchet and deleted the script.
Console command agent-comms-ratchet requires --root, --base and --head.
The existing Measure family now derives members through DeclaredFamily; there is
no parallel roster. Changed-file union and move/deletion accounting are retained.
Core workflow invokes the installed command. Newest owner override wins over old
required-CI docs: manual workflow only, no branch protection or merge gates touched.

Built and installed the wheel into an owned isolated environment using prepared
runtime dependencies (no editable source). Eleven actual Git/console tests pass,
including both Comms and Toad source roots and exclusion of unrelated source.
Paired Toad collector is in /home/ts/wt/toad-tr0-test-collection-20260928; discovered
264 tests and its first real mounted pilot passes, including isolated network
namespace with loopback. Full Toad triage/paired pinned acceptance follows there.
No live runtime/provider calls or extra agents.
